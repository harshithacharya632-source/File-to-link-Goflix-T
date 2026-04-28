FROM python:3.10.8-slim-bullseye
RUN apt update && apt upgrade -y
RUN apt install git -y
RUN apt-get update && apt-get install -y ffmpeg
COPY requirements.txt /requirements.txt
RUN pip3 install -U pip && pip3 install -U -r requirements.txt
RUN mkdir /CantarellaBots
WORKDIR /CantarellaBots
COPY . /CantarellaBots
CMD ["python", "bot.py"]
