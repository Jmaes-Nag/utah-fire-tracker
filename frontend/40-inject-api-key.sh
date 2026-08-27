#!/bin/sh
set -e

# The default nginx:alpine entrypoint runs scripts in /docker-entrypoint.d/
# We use this to inject the CARTO_API_KEY environment variable into the static JS file.

if [ -f "/usr/share/nginx/html/app.js" ]; then
    # Replace the placeholder with the actual API key. If CARTO_API_KEY is unset, replaces with empty string.
    sed -i "s|__CARTO_API_KEY__|${CARTO_API_KEY:-}|g" /usr/share/nginx/html/app.js
fi
