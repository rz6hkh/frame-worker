FROM python:3.12-slim
# pillow-heif - без него не читается 6.5 тыс. HEIC с айфонов (22% архива):
# в PowerShell их открывал WIC, у Pillow своего декодера нет.
RUN pip install --no-cache-dir psycopg[binary]==3.2.* requests==2.32.* pillow==11.* numpy==2.* pillow-heif==0.* websockets==15.*
WORKDIR /app
COPY *.py *.sql ./
ENV PYTHONUNBUFFERED=1
