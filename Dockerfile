FROM python:3.11-slim

# home directory ...
RUN mkdir -p /footage-archive
WORKDIR /footage-archive

# install required libraries ...
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# install required python packages ...
COPY requirements.txt /footage-archive

#RUN --mount=type=secret,id=pip,target=/etc/pip.conf pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
# copy code ...
COPY ./ /footage-archive

RUN useradd -m appuser
RUN chown -R appuser:appuser /footage-archive
USER appuser

ENV TZ="Europe/Berlin"
ENV PATH=~/.local/bin:$PATH

CMD python app.py
