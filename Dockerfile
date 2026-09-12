FROM ghcr.io/telegrammessenger/proxy-bot-api:latest

ENV PORT=8081
EXPOSE 8081

ENV API_ID=${API_ID}
ENV API_HASH=${API_HASH}

CMD ["sh", "-c", "./telegram-bot-api --api-id $API_ID --api-hash $API_HASH --http-port $PORT"]

