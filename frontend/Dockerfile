FROM nginx:alpine

# Copy custom nginx config template for environment variable substitution
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Add an entrypoint script to inject the API key
COPY 40-inject-api-key.sh /docker-entrypoint.d/40-inject-api-key.sh
RUN chmod +x /docker-entrypoint.d/40-inject-api-key.sh

# Copy static frontend files
COPY index.html sources.html styles.css app.js /usr/share/nginx/html/

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
