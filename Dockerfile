#FROM python:3.9.2-slim-buster
FROM python:3.10.15-slim-bullseye


WORKDIR /app    
RUN apt-get update
RUN apt-get install openssl -y
RUN apt-get install -y procps 
RUN apt-get install -y python3-pip
RUN apt-get install -y python3-venv
RUN apt-get install -y git
RUN apt-get install -y libgl1-mesa-glx
RUN apt-get install -y libportaudio2 
RUN apt-get install -y libportaudiocpp0 
RUN apt-get install -y portaudio19-dev
#RUN apt-get install -y python-pyaudio
#RUN apt-get install -y libportaudio19-dev
#RUN apt-get install -y alsa-base 
#RUN apt-get install -y alsa 
RUN apt-get install -y libasound2 
RUN apt-get install -y libasound2-dev 
RUN apt-get install -y alsa-utils  
RUN apt-get install -y alsa-tools 
RUN apt-get install -y  libsndfile1 
RUN apt-get install -y ffmpeg 


RUN mkdir /run/mosquitto/
RUN apt-get install -y mosquitto  
RUN apt-get install -y mosquitto-clients
COPY certs/ca.crt /etc/mosquitto/certs/ca.crt
COPY certs/server.crt /etc/mosquitto/certs/server.crt
COPY certs/server.key /etc/mosquitto/certs/server.key
RUN echo "allow_anonymous false" >> /etc/mosquitto/mosquitto.conf
RUN echo "password_file /etc/mosquitto/passwd" >> /etc/mosquitto/mosquitto.conf 
#RUN echo "listener 1883 0.0.0.0" >> /etc/mosquitto/mosquitto.conf
RUN echo "listener 8883" >> /etc/mosquitto/mosquitto.conf
RUN echo "cafile /app/certs/certs/ca.crt" >> /etc/mosquitto/mosquitto.conf
RUN echo "certfile /app/certs/certs/server.crt" >> /etc/mosquitto/mosquitto.conf
RUN echo "keyfile /app/certs/certs/server.key" >> /etc/mosquitto/mosquitto.conf
RUN echo "require_certificate true" >> /etc/mosquitto/mosquitto.conf
RUN touch  /etc/mosquitto/passwd
RUN mosquitto_passwd -b /etc/mosquitto/passwd esp32s3-1 ast-esp32s3-1
RUN mosquitto_passwd -b /etc/mosquitto/passwd esp32s3-2 ast-esp32s3-2
RUN mosquitto_passwd -b /etc/mosquitto/passwd rasp4 ast-rasp4
RUN mosquitto_passwd -b /etc/mosquitto/passwd pc_client ast-pc_client
RUN mosquitto_passwd -b /etc/mosquitto/passwd laptop_client ast-laptop_client
RUN chown mosquitto /run/mosquitto
RUN chmod 777 /run/mosquitto
RUN chogrp mosquitto /etc/mosquitto/certs/server.key

#RUN service   mosquitto restart
#RUN service   mosquitto start
#RUN systemctl enable mosquitto.service
#RUN systemctl restart mosquitto.service
RUN rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/Gsarant/ast_audio_net.git
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r ast_audio_net/requirements.txt


RUN mkdir /app/ast_audio_net/certs/certs
COPY certs/certs/ca.crt /app/ast_audio_net/certs/certs/ca.crt
COPY certs/certs/rasp5.crt /app/ast_audio_net/certs/certs/rasp5.crt
COPY certs/certs/rasp5.key /app/ast_audio_net/certs/certs/rasp5.key

COPY parameters.json /app/parameters.json

#COPY ast_app /etc/init.d/ast_app
#COPY ast_dash /etc/init.d/ast_dash
#RUN chmod +x /etc/init.d/ast_app
#RUN chmod +x /etc/init.d/ast_dash
COPY run_ast_services.sh ./
RUN chmod +x run_ast_services.sh

#RUN apt-get install git-lfs
#RUN git clone https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593
COPY ast-finetuned-audioset-10-10-0.4593 /app/ast_audio_net/ast-finetuned-audioset-10-10-0.4593
# Setup an app user so the container doesn't run as the root user
#RUN useradd ast_user
#USER ast_user

RUN mkdir -p /app/ast_audio_net/assets
RUN chmod 777 /app/ast_audio_net/assets


RUN mkdir -p /app/ast_audio_net/ast_audio_db
RUN chmod 777 /app/ast_audio_net/ast_audio_db

RUN mkdir -p /app/ast_audio_net/logs_ast_app
RUN chmod 777 /app/ast_audio_net/logs_ast_app

#EXPOSE 1883
EXPOSE 8883
EXPOSE 8050
CMD ["/bin/bash", "-c", "./run_ast_services.sh"]
#CMD ["cd", "ast_audio_net","python", "ast_src/main.py" ,"cd", "ast_dash", "python", "ast_app"]
#https://www.baeldung.com/linux/docker-cmd-multiple-commands