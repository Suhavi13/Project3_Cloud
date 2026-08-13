import azure.functions as func
import bcrypt
import io
import json
import jwt
import logging
import os
import pandas as pd

from datetime import datetime, timedelta, timezone
from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

app = func.FunctionApp()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

# -----------------------------
# Storage / processing settings
# -----------------------------
DATA_CONTAINER_NAME = os.environ.get("DATA_CONTAINER_NAME", "datasets")
BLOB_NAME = os.environ.get("BLOB_NAME", "All_Diets.csv")
CLEANED_BLOB_NAME = os.environ.get(
    "CLEANED_BLOB_NAME", "processed/All_Diets_clean.csv"
)
CACHE_BLOB_NAME = os.environ.get(
    "CACHE_BLOB_NAME", "cache/insights.json"
)
SOURCE_BLOB_PATH = f"{DATA_CONTAINER_NAME}/{BLOB_NAME}"

# -----------------------------
# Authentication settings
# -----------------------------
AUTH_DATABASE_NAME = os.environ.get("AUTH_DATABASE_NAME", "dietappdb")
AUTH_CONTAINER_NAME = os.environ.get("AUTH_CONTAINER_NAME", "users")
JWT_SECRET = os.environ.get("JWT_SECRET")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")

_users_container = None


# ============================================================
# Blob Storage helpers
# ============================================================
def get_container_client():
    """Return the configured Blob container client."""
    connection_string = os.environ.get("DIET_STORAGE_CONNECTION")
    if not connection_string:
        raise RuntimeError("DIET_STORAGE_CONNECTION is not configured.")

    blob_service_client = BlobServiceClient.from_connection_string(
        connection_string
    )
    return blob_service_client.get_container_client(DATA_CONTAINER_NAME)


def get_blob_client(blob_name: str):
    return get_container_client().get_blob_client(blob_name)


# ============================================================
# Data processing helpers
# ============================================================
def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = [
        "Diet_type",
        "Recipe_name",
        "Cuisine_type",
        "Protein(g)",
        "Carbs(g)",
        "Fat(g)",
    ]

    missing = [column for column in required_cols if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    cleaned = df.copy()
    nutrition_cols = ["Protein(g)", "Carbs(g)", "Fat(g)"]

    for column in nutrition_cols:
        cleaned[column] = pd.to_numeric(
            cleaned[column], errors="coerce"
        ).fillna(0)

    for column in ["Diet_type", "Recipe_name", "Cuisine_type"]:
        cleaned[column] = (
            cleaned[column]
            .fillna("")
            .astype(str)
            .str.strip()
        )

    return cleaned


def calculate_insights(df: pd.DataFrame, processing_seconds: float) -> dict:
    nutrition_cols = ["Protein(g)", "Carbs(g)", "Fat(g)"]

    avg_macros = df.groupby("Diet_type")[nutrition_cols].mean().round(2)

    top_protein = (
        df.nlargest(10, "Protein(g)")[["Recipe_name", "Protein(g)"]]
        .to_dict("records")
    )

    diet_counts = df["Diet_type"].value_counts().to_dict()
    cuisine_counts = df["Cuisine_type"].value_counts().head(10).to_dict()
    correlations = df[nutrition_cols].corr().round(3).to_dict()

    return {
        "status": "success",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "processing_time": round(processing_seconds, 6),
        "total_records": len(df),
        "average_macronutrients": avg_macros.to_dict(),
        "top_protein_recipes": top_protein,
        "diet_counts": diet_counts,
        "cuisine_counts": cuisine_counts,
        "correlations": correlations,
        "diet_types": sorted(
            [value for value in df["Diet_type"].unique().tolist() if value]
        ),
        "source_blob": BLOB_NAME,
        "cleaned_blob": CLEANED_BLOB_NAME,
        "cache_blob": CACHE_BLOB_NAME,
    }


# ============================================================
# Cosmos DB / authentication helpers
# ============================================================
def get_users_container():
    global _users_container

    if _users_container is not None:
        return _users_container

    connection_string = os.environ.get("COSMOS_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError("COSMOS_CONNECTION_STRING is not configured.")

    client = CosmosClient.from_connection_string(connection_string)
    database = client.create_database_if_not_exists(id=AUTH_DATABASE_NAME)
    _users_container = database.create_container_if_not_exists(
        id=AUTH_CONTAINER_NAME,
        partition_key=PartitionKey(path="/email"),
    )
    return _users_container


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    )
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


def create_auth_token(user: dict) -> str:
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is not configured.")

    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "name": user["name"],
        "iat": now,
        "exp": now + timedelta(hours=1),
    }

    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_authenticated_user(req: func.HttpRequest):
    """Validate Authorization: Bearer <JWT>."""
    if not JWT_SECRET:
        return None, func.HttpResponse(
            json.dumps({"error": "Authentication is not configured."}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )

    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, func.HttpResponse(
            json.dumps({"error": "Authentication required."}),
            status_code=401,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )

    token = auth_header[7:].strip()

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
        )
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, func.HttpResponse(
            json.dumps({"error": "Session expired. Please log in again."}),
            status_code=401,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except jwt.InvalidTokenError:
        return None, func.HttpResponse(
            json.dumps({"error": "Invalid authentication token."}),
            status_code=401,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


def get_json_body(req: func.HttpRequest):
    """Return parsed JSON or a 400 response."""
    try:
        return req.get_json(), None
    except ValueError:
        return None, func.HttpResponse(
            json.dumps({"error": "Request body must contain valid JSON."}),
            status_code=400,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


# ============================================================
# Phase 3 processing: clean/calculate only when source CSV changes
# ============================================================
@app.blob_trigger(
    arg_name="source_blob",
    path=SOURCE_BLOB_PATH,
    connection="DIET_STORAGE_CONNECTION",
    source="EventGrid"
)
def process_all_diets(source_blob: func.InputStream) -> None:
    started_at = datetime.now(timezone.utc)
    logging.info("PHASE 3: All_Diets.csv change detected. Starting processing.")
    logging.info("Triggered by blob: %s", source_blob.name)

    try:
        raw_bytes = source_blob.read()
        df = pd.read_csv(io.BytesIO(raw_bytes))
        logging.info("Loaded %s rows from the source CSV.", len(df))

        cleaned_df = clean_data(df)

        cleaned_csv = cleaned_df.to_csv(index=False).encode("utf-8")
        get_blob_client(CLEANED_BLOB_NAME).upload_blob(
            cleaned_csv,
            overwrite=True,
            content_settings=ContentSettings(content_type="text/csv"),
        )
        logging.info("Saved cleaned dataset to %s.", CLEANED_BLOB_NAME)

        processing_seconds = (
            datetime.now(timezone.utc) - started_at
        ).total_seconds()
        cache_payload = calculate_insights(cleaned_df, processing_seconds)

        get_blob_client(CACHE_BLOB_NAME).upload_blob(
            json.dumps(cache_payload, ensure_ascii=False),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

        final_seconds = (
            datetime.now(timezone.utc) - started_at
        ).total_seconds()
        logging.info(
            "PHASE 3: Processing complete in %.3fs. Cache saved to %s.",
            final_seconds,
            CACHE_BLOB_NAME,
        )
    except Exception:
        logging.exception("PHASE 3: Failed to process All_Diets.csv.")
        raise


# ============================================================
# Authentication API
# ============================================================
@app.route(
    route="auth/config",
    methods=["GET", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def auth_config(req: func.HttpRequest) -> func.HttpResponse:
    """Return public authentication configuration needed by the frontend."""
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    return func.HttpResponse(
        json.dumps(
            {
                "status": "success",
                "google_enabled": bool(GOOGLE_CLIENT_ID),
                # OAuth client IDs are public identifiers, not secrets.
                "google_client_id": GOOGLE_CLIENT_ID or "",
            }
        ),
        status_code=200,
        mimetype="application/json",
        headers=CORS_HEADERS,
    )


@app.route(
    route="auth/register",
    methods=["POST", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def register_user(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    body, error_response = get_json_body(req)
    if error_response:
        return error_response

    try:
        name = str(body.get("name", "")).strip()
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))

        if not name:
            return func.HttpResponse(
                json.dumps({"error": "Name is required."}),
                status_code=400,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        if not email or "@" not in email:
            return func.HttpResponse(
                json.dumps({"error": "A valid email is required."}),
                status_code=400,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        if len(password) < 8:
            return func.HttpResponse(
                json.dumps({"error": "Password must be at least 8 characters."}),
                status_code=400,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        container = get_users_container()

        try:
            container.read_item(item=email, partition_key=email)
            return func.HttpResponse(
                json.dumps({"error": "An account with this email already exists."}),
                status_code=409,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )
        except CosmosResourceNotFoundError:
            pass

        user = {
            "id": email,
            "email": email,
            "name": name,
            "password_hash": hash_password(password),
            "provider": "password",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Plaintext password is never written to Cosmos DB.
        container.create_item(body=user)
        token = create_auth_token(user)

        logging.info("Registered user: %s", email)

        return func.HttpResponse(
            json.dumps(
                {
                    "status": "success",
                    "token": token,
                    "user": {"name": name, "email": email},
                }
            ),
            status_code=201,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except Exception as error:
        logging.exception("Registration failed.")
        return func.HttpResponse(
            json.dumps({"error": str(error)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


@app.route(
    route="auth/login",
    methods=["POST", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def login_user(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    body, error_response = get_json_body(req)
    if error_response:
        return error_response

    try:
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))

        container = get_users_container()

        try:
            user = container.read_item(item=email, partition_key=email)
        except CosmosResourceNotFoundError:
            return func.HttpResponse(
                json.dumps({"error": "Invalid email or password."}),
                status_code=401,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        password_hash = user.get("password_hash")
        if not password_hash:
            return func.HttpResponse(
                json.dumps({"error": "This account uses third-party login."}),
                status_code=400,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        if not verify_password(password, password_hash):
            return func.HttpResponse(
                json.dumps({"error": "Invalid email or password."}),
                status_code=401,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        token = create_auth_token(user)
        logging.info("User logged in: %s", email)

        return func.HttpResponse(
            json.dumps(
                {
                    "status": "success",
                    "token": token,
                    "user": {
                        "name": user["name"],
                        "email": user["email"],
                    },
                }
            ),
            status_code=200,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except Exception as error:
        logging.exception("Login failed.")
        return func.HttpResponse(
            json.dumps({"error": str(error)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


@app.route(
    route="auth/google",
    methods=["POST", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def google_login(req: func.HttpRequest) -> func.HttpResponse:
    """Verify a Google Identity Services ID token and create an app session."""
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    if not GOOGLE_CLIENT_ID:
        return func.HttpResponse(
            json.dumps({"error": "Google login is not configured."}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )

    body, error_response = get_json_body(req)
    if error_response:
        return error_response

    credential = str(body.get("credential", "")).strip()
    if not credential:
        return func.HttpResponse(
            json.dumps({"error": "Google credential is required."}),
            status_code=400,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )

    try:
        idinfo = google_id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )

        email = str(idinfo.get("email", "")).strip().lower()
        name = str(idinfo.get("name", "")).strip()
        google_sub = str(idinfo.get("sub", "")).strip()
        email_verified = idinfo.get("email_verified") is True

        if not email or not google_sub or not email_verified:
            return func.HttpResponse(
                json.dumps({"error": "Google account could not be verified."}),
                status_code=401,
                mimetype="application/json",
                headers=CORS_HEADERS,
            )

        if not name:
            name = email.split("@", 1)[0]

        container = get_users_container()

        try:
            user = container.read_item(item=email, partition_key=email)

            existing_google_sub = str(user.get("google_sub", "")).strip()
            if existing_google_sub and existing_google_sub != google_sub:
                return func.HttpResponse(
                    json.dumps({"error": "This email is linked to a different Google account."}),
                    status_code=409,
                    mimetype="application/json",
                    headers=CORS_HEADERS,
                )

            user["google_sub"] = google_sub
            user["provider"] = (
                "password+google" if user.get("password_hash") else "google"
            )
            user["name"] = user.get("name") or name
            user["last_google_login_at"] = datetime.now(timezone.utc).isoformat()
            container.upsert_item(body=user)

        except CosmosResourceNotFoundError:
            user = {
                "id": email,
                "email": email,
                "name": name,
                "provider": "google",
                "google_sub": google_sub,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_google_login_at": datetime.now(timezone.utc).isoformat(),
            }
            container.create_item(body=user)

        token = create_auth_token(user)
        logging.info("Google user logged in: %s", email)

        return func.HttpResponse(
            json.dumps(
                {
                    "status": "success",
                    "token": token,
                    "user": {
                        "name": user["name"],
                        "email": user["email"],
                    },
                }
            ),
            status_code=200,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )

    except ValueError:
        logging.warning("Rejected invalid Google ID token.")
        return func.HttpResponse(
            json.dumps({"error": "Invalid Google sign-in token."}),
            status_code=401,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except Exception as error:
        logging.exception("Google login failed.")
        return func.HttpResponse(
            json.dumps({"error": str(error)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


@app.route(
    route="auth/me",
    methods=["GET", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def auth_me(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    user, error_response = get_authenticated_user(req)
    if error_response:
        return error_response

    return func.HttpResponse(
        json.dumps(
            {
                "status": "success",
                "user": {
                    "name": user.get("name"),
                    "email": user.get("email"),
                },
            }
        ),
        status_code=200,
        mimetype="application/json",
        headers=CORS_HEADERS,
    )


# ============================================================
# Protected dashboard API
# ============================================================
@app.route(
    route="insights",
    methods=["GET", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def get_insights(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    user, error_response = get_authenticated_user(req)
    if error_response:
        return error_response

    logging.info(
        "Insights requested by authenticated user: %s",
        user.get("email"),
    )
    logging.info("Insights endpoint called - reading cached results only.")

    started_at = datetime.now(timezone.utc)

    try:
        cache_bytes = get_blob_client(CACHE_BLOB_NAME).download_blob().readall()
        response = json.loads(cache_bytes.decode("utf-8"))

        cache_read_time = (
            datetime.now(timezone.utc) - started_at
        ).total_seconds()

        response["execution_time"] = round(cache_read_time, 6)
        response["served_from_cache"] = True

        logging.info(
            "Returning cached insights in %.3fs. No CSV processing performed.",
            cache_read_time,
        )

        return func.HttpResponse(
            json.dumps(response),
            status_code=200,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except ResourceNotFoundError:
        return func.HttpResponse(
            json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Insights cache has not been created yet. "
                        "Upload or replace All_Diets.csv to trigger processing."
                    ),
                }
            ),
            status_code=503,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except Exception as error:
        logging.exception("Failed to read cached insights.")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(error)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


@app.route(
    route="recipes",
    methods=["GET", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def get_recipes(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    user, error_response = get_authenticated_user(req)
    if error_response:
        return error_response

    logging.info("Recipe search requested by: %s", user.get("email"))
    logging.info("Recipes endpoint called - reading cleaned dataset.")

    try:
        diet = (req.params.get("diet") or "").strip()
        keyword = (req.params.get("search") or "").strip()

        try:
            page = int(req.params.get("page", "1"))
        except ValueError:
            page = 1

        try:
            page_size = int(req.params.get("page_size", "10"))
        except ValueError:
            page_size = 10

        page = max(page, 1)
        page_size = max(1, min(page_size, 50))

        cleaned_bytes = get_blob_client(CLEANED_BLOB_NAME).download_blob().readall()
        df = pd.read_csv(io.BytesIO(cleaned_bytes))
        filtered_df = df.copy()

        if diet and diet.lower() != "all":
            filtered_df = filtered_df[
                filtered_df["Diet_type"].astype(str).str.casefold()
                == diet.casefold()
            ]

        if keyword:
            recipe_match = filtered_df["Recipe_name"].astype(str).str.contains(
                keyword, case=False, na=False, regex=False
            )
            cuisine_match = filtered_df["Cuisine_type"].astype(str).str.contains(
                keyword, case=False, na=False, regex=False
            )
            diet_match = filtered_df["Diet_type"].astype(str).str.contains(
                keyword, case=False, na=False, regex=False
            )
            filtered_df = filtered_df[recipe_match | cuisine_match | diet_match]

        total_results = len(filtered_df)
        total_pages = (
            (total_results + page_size - 1) // page_size
            if total_results > 0
            else 0
        )

        if total_pages > 0 and page > total_pages:
            page = total_pages

        start = (page - 1) * page_size
        end = start + page_size
        page_df = filtered_df.iloc[start:end]

        columns = [
            "Recipe_name",
            "Diet_type",
            "Cuisine_type",
            "Protein(g)",
            "Carbs(g)",
            "Fat(g)",
        ]
        recipes = page_df[columns].to_dict(orient="records")

        response = {
            "status": "success",
            "filters": {
                "diet": diet or "all",
                "search": keyword,
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_results": total_results,
                "total_pages": total_pages,
                "has_previous": page > 1,
                "has_next": total_pages > 0 and page < total_pages,
            },
            "recipes": recipes,
        }

        return func.HttpResponse(
            json.dumps(response),
            status_code=200,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except ResourceNotFoundError:
        return func.HttpResponse(
            json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Cleaned dataset does not exist yet. "
                        "Upload All_Diets.csv first."
                    ),
                }
            ),
            status_code=503,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )
    except Exception as error:
        logging.exception("Failed to search recipes.")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(error)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS,
        )


@app.route(
    route="health",
    methods=["GET", "OPTIONS"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=CORS_HEADERS)

    return func.HttpResponse(
        json.dumps(
            {
                "status": "healthy",
                "service": "Diet Analysis API",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ),
        status_code=200,
        mimetype="application/json",
        headers=CORS_HEADERS,
    )
