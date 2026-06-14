from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    anthropic_api_key: str
    openai_api_key: str
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"
    semaphore_limit: int = 5
    sweep_rate_per_minute: int = 10
    source_authority_path: str = "source_authority.yaml"
    mcp_port: int = 8001
    api_port: int = 8000

    class Config:
        env_file = ".env"
        extra = "ignore"  # the shared .env also carries app/ settings keys


settings = Settings()
