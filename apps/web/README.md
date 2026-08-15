# Web

O frontend Next.js ainda não foi iniciado. Primeiro estamos estabilizando os contratos HTTP e o
executor protegido; isso evita construir telas sobre respostas que ainda mudariam rapidamente.

Quando esta etapa começar, os tipos TypeScript serão gerados do OpenAPI publicado pelo FastAPI.
As telas planejadas são visão do sistema, playground, trace, conectores, avaliações e system card.

Até lá, o Swagger em `http://127.0.0.1:8000/docs` é a interface de inspeção do backend.
