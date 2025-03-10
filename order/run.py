from __init__ import create_app
import logging
from kafka_utils import start_stock_listener, start_payment_listener
import threading

app = create_app()

# For debugging purposes
import debugpy
debugpy.listen(("0.0.0.0", 5678))


threading.Thread(target=start_stock_listener, args=(app,), daemon=True).start()
threading.Thread(target=start_payment_listener, args=(app,), daemon=True).start()

if __name__ == '__main__':
    #import uvicorn
    #uvicorn.run(asgi_app, host="0.0.0.0", port=8000, log_level="debug")
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)