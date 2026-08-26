FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

ENV TZ=Asia/Shanghai

RUN rm -f /etc/nginx/conf.d/default.conf

COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --chown=nginx:nginx frontend-dist/ /usr/share/nginx/html/

RUN test -s /usr/share/nginx/html/index.html

USER nginx

EXPOSE 8080

ENTRYPOINT ["nginx", "-g", "daemon off;"]
