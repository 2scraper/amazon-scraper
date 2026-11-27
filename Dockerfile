# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium — for a CI canary run or a scheduled job,
# not required for local development (`pip install` directly is simpler there).
#
#   docker build -t amazon-scraper .
#   docker run --rm -v "$PWD/out:/out" amazon-scraper \
#     --url "https://www.amazon.com/s?k=wireless+headphones" \
#     --pages 3 --out /out/headphones
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
# libatomic1 first: Playwright's bundled Node driver needs it just to run
# `playwright install`, i.e. before that command can install any system
# dependency itself (the slim base image does not ship it; the build
# failed with "libatomic.so.1: cannot open shared object file").
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module the entrypoint imports, transitively. This list had fallen
# behind: proxy_pool.py was missing while the engine imports it at module
# level, so the image died with ModuleNotFoundError on every invocation,
# `--help` included. Nothing noticed, because CI never built the image.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py diff_runs.py proxy_pool.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
