# All-in-one image: Neo4j + scraper pipeline + web app in one container.
# Build context = repository root:  docker build -f deploy/Dockerfile .
FROM python:3.13-slim

# Java runtime and Neo4j server, copied from the official Neo4j image.
COPY --from=neo4j:5.26-community /opt/java/openjdk /opt/java/openjdk
COPY --from=neo4j:5.26-community /var/lib/neo4j /var/lib/neo4j
ENV JAVA_HOME=/opt/java/openjdk \
    NEO4J_HOME=/var/lib/neo4j \
    PATH=/var/lib/neo4j/bin:/opt/java/openjdk/bin:$PATH \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data /logs

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mse_graph ./mse_graph
COPY data/sample ./data/sample
COPY data/manual ./data/manual
# the MCS Group dataset: groups.curated_score checks the detected groups against it (#28)
COPY data/mcs/graph ./data/mcs/graph
COPY data/discovery ./data/discovery
# hand decisions on identities (#37, #6) and the nominee holders list (#39)
COPY data/overrides ./data/overrides
COPY data/reference ./data/reference
COPY deploy/start.sh /start.sh

CMD ["/bin/bash", "/start.sh"]
