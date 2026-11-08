FROM python:3.13-alpine
RUN pip install --no-cache-dir mcp==2.3.0 httpx==0.28.1 icalendar==7.3.0 vobject==0.9.9 \
 && adduser -D -u 1000 mcp
COPY server.py /app/server.py
USER mcp
EXPOSE 8080
CMD ["python", "/app/server.py"]
