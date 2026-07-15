from flask import Flask, send_file
import os

app = Flask(__name__)

@app.route('/')
def index():
    return send_file('index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_file(path)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
