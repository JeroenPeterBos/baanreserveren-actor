"""
This module serves as the entry point for executing the Apify Actor. It handles the configuration of logging
settings. The `main()` coroutine is then executed using `asyncio.run()`.

Feel free to modify this file to suit your specific needs.
"""

import asyncio
import logging

from apify.log import ActorLogFormatter

from .baanreserveren import main

# Configure loggers
handler = logging.StreamHandler()
handler.setFormatter(ActorLogFormatter())

apify_client_logger = logging.getLogger("apify_client")
apify_client_logger.setLevel(logging.INFO)
apify_client_logger.addHandler(handler)

apify_logger = logging.getLogger("apify")
apify_logger.setLevel(logging.DEBUG)
apify_logger.addHandler(handler)

main_logger = logging.getLogger("src.baanreserveren")
main_logger.setLevel(logging.DEBUG)
main_logger.addHandler(handler)

# The actor timeout used to be a fixed 3 minutes here, before the input was
# known. main() now applies it after reading the input, and extends it when
# wait_until / retry_for_seconds need longer than 3 minutes.
asyncio.run(main())
