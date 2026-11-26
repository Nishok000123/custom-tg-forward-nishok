FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# CRITICAL: Create a non-root user with UID 1000 for Hugging Face compatibility
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Set working directory inside the user's home folder
WORKDIR $HOME/app

# Copy requirements file and change ownership to the custom user
COPY --chown=user requirements.txt .

# Install Python dependencies safely under the user profile
RUN pip3 install --no-cache-dir -U pip && \
    pip3 install --no-cache-dir -U -r requirements.txt

# Copy the rest of the application code and set permissions
COPY --chown=user . .

# CRITICAL: Tell Hugging Face which port your main.py web server is listening on
EXPOSE 7860

# Command to run the bot
CMD ["python3", "main.py"]