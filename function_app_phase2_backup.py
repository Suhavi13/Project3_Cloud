import azure.functions as func
import logging
import json
import pandas as pd
import io
import os
from datetime import datetime
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type"
}

def get_blob_client():
    """Get blob client for Azure Storage"""
    connection_string = os.environ.get("STORAGE_CONNECTION_STRING")
    container_name = os.environ.get("CONTAINER_NAME", "datasets")
    blob_name = os.environ.get("BLOB_NAME", "All_Diets.csv")
    
    blob_service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = blob_service_client.get_container_client(container_name)
    return container_client.get_blob_client(blob_name)

def load_data():
    """Load data from Azure Blob Storage"""
    try:
        blob_client = get_blob_client()
        stream = blob_client.download_blob().readall()
        df = pd.read_csv(io.BytesIO(stream))
        return df
    except Exception as e:
        logging.error(f"Error loading data: {e}")
        return None

@app.route(route="insights", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_insights(req: func.HttpRequest) -> func.HttpResponse:
    """Return nutritional insights as JSON for dashboard"""
    logging.info("📊 Insights endpoint called")
    
    try:
        start_time = datetime.now()
        
        # Load data
        df = load_data()
        if df is None:
            return func.HttpResponse(
                json.dumps({"error": "Failed to load data"}),
                status_code=500,
                mimetype="application/json",
                headers=CORS_HEADERS
            )
        
        # Clean data
        nutrition_cols = ['Protein(g)', 'Carbs(g)', 'Fat(g)']
        df[nutrition_cols] = df[nutrition_cols].fillna(0)
        
        # Calculate averages by diet type
        avg_macros = df.groupby('Diet_type')[nutrition_cols].mean().round(2)
        
        # Get top protein recipes
        top_protein = df.nlargest(10, 'Protein(g)')[['Recipe_name', 'Protein(g)']].to_dict('records')
        
        # Count recipes by diet type
        diet_counts = df['Diet_type'].value_counts().to_dict()
        
        # Get cuisine distribution
        cuisine_counts = df['Cuisine_type'].value_counts().head(10).to_dict()
        
        # Calculate correlations
        correlations = df[nutrition_cols].corr().round(3).to_dict()
        
        # Execution time
        execution_time = (datetime.now() - start_time).total_seconds()
        
        response = {
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "execution_time": execution_time,
            "total_records": len(df),
            "average_macronutrients": avg_macros.to_dict(),
            "top_protein_recipes": top_protein,
            "diet_counts": diet_counts,
            "cuisine_counts": cuisine_counts,
            "correlations": correlations,
            "diet_types": df['Diet_type'].unique().tolist()
        }
        
        logging.info(f"✅ Insights generated in {execution_time:.2f}s")
        return func.HttpResponse(
            json.dumps(response),
            status_code=200,
            mimetype="application/json",
            headers=CORS_HEADERS
        )
        
    except Exception as e:
        logging.error(f"❌ Error: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json",
            headers=CORS_HEADERS
        )

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Health check endpoint"""
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "service": "Diet Analysis API",
            "timestamp": datetime.now().isoformat()
        }),
        status_code=200,
        mimetype="application/json",
        headers=CORS_HEADERS
    )