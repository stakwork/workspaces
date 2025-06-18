#!/usr/bin/env python3
"""
Workspace API - Main entry point

A Flask application for managing code-server workspaces in Kubernetes.
"""

import logging
import sys
from app import create_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/tmp/workspace-api.log')
    ]
)

logger = logging.getLogger(__name__)

def main():
    """Main entry point"""
    try:
        # Create the Flask application
        app = create_app()
        
        # Add health check endpoint
        @app.route('/health')
        def health_check():
            return {'status': 'healthy', 'service': 'workspace-api'}, 200
        
        @app.route('/ready')
        def readiness_check():
            """Readiness probe - check if we can connect to Kubernetes"""
            try:
                from app.config import app_config
                # Try to list namespaces to verify K8s connectivity
                app_config.core_v1.list_namespace(limit=1)
                return {'status': 'ready', 'service': 'workspace-api'}, 200
            except Exception as e:
                logger.error(f"Readiness check failed: {e}")
                return {'status': 'not ready', 'error': str(e)}, 503
        
        @app.route('/')
        def root():
            return {
                'service': 'workspace-api',
                'version': '1.0.0',
                'status': 'running',
                'endpoints': {
                    'health': '/health',
                    'ready': '/ready',
                    'auth': '/api/auth',
                    'workspaces': '/api/workspaces'
                }
            }
        
        # Global error handlers
        @app.errorhandler(404)
        def not_found(error):
            return {'error': 'Endpoint not found'}, 404
        
        @app.errorhandler(500)
        def internal_error(error):
            logger.error(f"Internal server error: {error}")
            return {'error': 'Internal server error'}, 500
        
        @app.errorhandler(Exception)
        def handle_exception(e):
            logger.error(f"Unhandled exception: {e}", exc_info=True)
            return {'error': 'An unexpected error occurred'}, 500
        
        logger.info("Starting Workspace API server...")
        
        # Run the application
        app.run(
            host='0.0.0.0',
            port=3000,
            debug=False,  # Set to False in production
            threaded=True
        )
        
    except Exception as e:
        logger.error(f"Failed to start application: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()