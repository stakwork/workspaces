from flask import Flask
from flask_cors import CORS
import logging

def create_app():
    app = Flask(__name__)
    CORS(app)
    
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Register blueprints
    from app.auth.routes import auth_bp
    from app.workspace.routes import workspace_bp
    from app.pool.routes import pool_bp
    from app.user.routes import user_bp
    
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(user_bp, url_prefix='/api/users')
    app.register_blueprint(workspace_bp, url_prefix='/api/workspaces')
    app.register_blueprint(pool_bp, url_prefix='/api/pools')
    
    return app