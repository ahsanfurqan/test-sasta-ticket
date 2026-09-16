# One image, two commands: the api serves, the worker drains. Same code, same deps --
# a reconciliation job that disagrees with the API about what a charge means is a whole
# class of bug we simply do not have this way.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a source edit does not reinstall the world.
COPY pyproject.toml README.md ./
COPY src/meter/__init__.py src/meter/__init__.py
RUN pip install -e ".[dev]"

COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY tests/ tests/
COPY loadtest/ loadtest/

EXPOSE 8000

CMD ["uvicorn", "meter.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
