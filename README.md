# fastapi-hello

Минимальный стартовый проект на FastAPI + Docker.

## Быстрый старт

```bash
docker compose up --build
```

Откройте [http://localhost:8000](http://localhost:8000)

## Маршруты

| Маршрут | Описание |
|---------|----------|
| `GET /` | Главная страница |
| `GET /api/ping` | Health-check (JSON) |
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc |

## Структура

```
fastapi-hello/
├── main.py              # FastAPI приложение
├── templates/
│   └── index.html       # Главная страница
├── static/              # Статические файлы (CSS, JS, изображения)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .dockerignore
```

## Локальный запуск (без Docker)

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
