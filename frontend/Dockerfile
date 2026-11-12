FROM nginx:alpine

# Trust store for verified HTTPS requests to the basemap provider.
RUN apk add --no-cache ca-certificates

# Copy custom nginx config template for environment variable substitution
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Copy static frontend files
COPY index.html sources.html styles.css app.js /usr/share/nginx/html/

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
