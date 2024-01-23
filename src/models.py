from typing import Literal
from pydantic import BaseModel, BaseSettings, Field


class Settings(BaseSettings):
    hp_username: str = Field(env="HP_UK_USERNAME", description="Helloprint username")
    hp_password: str = Field(env="HP_UK_PASSWORD", description="Helloprint password")
    headless: bool = Field(env="HEADLESS", default=True, description="Run browser in headless mode")
    execution_speed: float = Field(env="EXECUTION_SPEED", default=1.0, description="Execution speed multiplier")


class Screenshot(BaseModel):
    name: str = Field(description="Screenshot name")
    content: str = Field(description="Base64 encoded screenshot")
    contentType: Literal["image/png", "image/jpeg"] = Field(description="Screenshot content type")


class Output(BaseModel):
    screenshots: list[Screenshot]


class Input(BaseModel):
    onl_number: str = Field(default="1234567", description="ONL number")
    out_number: str = Field(default=None, description="OUT number")
    save_screenshots: bool = Field(default=False, description="Save screenshots to files in the key-value store")


if __name__ == "__main__":
    import json

    json_schema = Input.schema()

    # depending on the type set `editor` and depending on the default set `prefill``
    for field in json_schema["properties"].values():
        if field["type"] == "string":
            field["editor"] = "textfield"
            if field.get("default"):
                field["prefill"] = field["default"]

    json_schema["title"] = "Helloprint Order Screenshots"
    json_schema["schemaVersion"] = 1

    print(json.dumps(json_schema, indent=4))
