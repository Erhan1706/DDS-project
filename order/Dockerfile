FROM python:3.12-slim

WORKDIR /home/flask-app

RUN apt-get update && apt-get install -y netcat-openbsd
COPY ./requirements.txt .

RUN pip install -r requirements.txt

COPY . .
RUN chmod +x ./wait-for-kafka.sh

EXPOSE 5000

CMD ["./wait-for-kafka.sh"]