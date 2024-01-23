from typing import Literal
from pydantic import BaseModel, BaseSettings, Field


class Settings(BaseSettings):
    dry_run: bool = Field(ENV="DRY_RUN", default=True, description="Don't actually place the reservation")
    username: str = Field(env="BR_USERNAME", description="Baanreserveren username")
    password: str = Field(env="BR_PASSWORD", description="Baanreserveren password")
    headless: bool = Field(env="HEADLESS", default=True, description="Run browser in headless mode")


class Input(BaseModel):
    dry_run: bool = Field(default=True, description="Don't actually place the reservation")
    date: str = Field(description="The date to book a slot on, format: yyyy-mm-dd")
    opponent: Literal["vera", "koen"] = Field(default="vera", description="The opponent to book a slot with")
    times: list[str] = Field(
        default=["20:30", "19:45", "21:15", "19:00"],
        description="The times to try to book a slot on the leden banen",
    )
    leden_only: bool = Field(default=True, description="Only try to book a slot on the leden banen")
    non_leden_times: list[str] = Field(
        default=["20:15", "19:30", "21:00"],
        description="The times to try to book a slot on the non-leden banen",
    )


if __name__ == "__main__":
    import json

    json_schema = Input.schema()

    # depending on the type set `editor` and depending on the default set `prefill``
    for field in json_schema["properties"].values():
        if field["type"] == "string":
            if "enum" in field:
                field["editor"] = "select"
            else:
                field["editor"] = "textfield"
            if field.get("default"):
                field["prefill"] = field["default"]

        if field["type"] == "array":
            field["editor"] = "javascript"

    json_schema["title"] = "Baanreserveren Actor"
    json_schema["schemaVersion"] = 1

    print(json.dumps(json_schema, indent=4))
