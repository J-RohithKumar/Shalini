# Use full Debian base to support all runtimes
FROM debian:bullseye

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies: Python, Node.js, Java, Postgres, Maven
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    wget \
    build-essential \
    python3 \
    python3-pip \
    python3-venv \
    # PostgreSQL server + client
    postgresql \
    postgresql-contrib \
    postgresql-client \
    # Java JDK 17
    openjdk-17-jdk \
    # Maven for building Java
    maven \
    # Node.js + npm (from Debian repos, v20 may require NodeSource)
    nodejs \
    npm \
 && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application source
COPY . /app

# Build Java JAR if pom.xml exists
RUN if [ -f "/app/java/pom.xml" ]; then \
        cd /app/java && \
        mvn clean package -DskipTests && \
        cp target/writeright-java-1.0.0.jar /app/java/app.jar; \
    fi

# Expose ports:
# 8000 → FastAPI
# 5432 → PostgreSQL
# 3000 → Node.js frontend
EXPOSE 8000 5432 3000

# Start script: launches Postgres, FastAPI, and Node (Java only if JAR exists)
CMD service postgresql start && \
    python3 -m uvicorn backend.run_service:app --host 0.0.0.0 --port 8000 & \
    cd frontend && npm start & \
    java -jar /app/java/app.jar

