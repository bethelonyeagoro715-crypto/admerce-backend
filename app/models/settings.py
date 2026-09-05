from pydantic import BaseModel
from typing import Optional

class AppSetting(BaseModel):
    key: str
    value: str

class AppSettingsResponse(BaseModel):
    enable_delivery: bool = False
    enable_courier: bool = False
    enable_flipper: bool = False
    maintenance_mode: bool = False
    app_version: str = "1.0.0"