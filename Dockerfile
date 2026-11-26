FROM python:3.11-slim

WORKDIR /opt/patchpilot
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Generated tests run inside this container, so do not give them root.
RUN useradd --create-home patchpilot
USER patchpilot

# Mount the repository to work on here.
WORKDIR /work
ENTRYPOINT ["patchpilot"]
