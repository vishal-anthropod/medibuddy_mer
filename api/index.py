from vercel_wsgi import handle
from app import app as flask_app


def handler(event, context):
	return handle(event, context, flask_app)


