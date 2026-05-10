from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    anthropic_api_key: str
    openai_api_key: str
    semaphore_limit: int = 5
    mcp_port: int = 8001
    api_port: int = 8000

    class Config:
        env_file = ".env"


settings = Settings()
