"""Process-local model profiles. Credentials never enter task persistence."""
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .config import CloudConfig
from .gateway import CloudGateway


class ModelProfileError(ValueError):
    pass


class ProbeResponse(BaseModel):
    status: Literal['ok']


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(max_length=512)
    model: str = Field(min_length=1, max_length=160)
    api_key: SecretStr = Field(min_length=1, max_length=4096)
    response_mode: Literal['json_object', 'json_schema', 'none'] = 'json_object'
    output_token_field: Literal['max_tokens', 'max_completion_tokens'] = 'max_tokens'
    thinking: Literal['disabled', 'enabled'] | None = None


class ModelProfiles:
    def __init__(self):
        self._profiles = {}
        self._probing = set()

    def add(self, data):
        if len(self._profiles) >= 20:
            raise ModelProfileError('model_profile_limit_reached')
        try:
            config = CloudConfig(base_url=data.base_url, api_key=data.api_key.get_secret_value(),
                                 model=data.model, response_mode=data.response_mode,
                                 output_token_field=data.output_token_field, thinking=data.thinking)
        except ValueError:
            raise ModelProfileError('invalid_model_configuration') from None
        profile_id = str(uuid.uuid4())
        public = {'id': profile_id, 'name': data.name.strip(), 'model': config.model,
                  'base_url': config.base_url, 'credential_storage': 'memory_only'}
        self._profiles[profile_id] = (public, config)
        return dict(public)

    def list(self):
        return [dict(public) for public, _ in self._profiles.values()]

    def resolve(self, profile_id):
        try:
            return self._profiles[profile_id][1]
        except KeyError:
            raise ModelProfileError('model_profile_not_found') from None

    def clear(self):
        self._profiles.clear()

    async def probe(self, profile_id):
        config = self.resolve(profile_id)
        if profile_id in self._probing:
            raise ModelProfileError('model_probe_busy')
        self._probing.add(profile_id)
        try:
            gateway = CloudGateway(config)
            result = await gateway.generate({'task': 'Return status ok to verify structured model connectivity.'}, ProbeResponse)
            return {'status': result.data.status, 'model': config.model, 'model_calls': gateway.calls}
        finally:
            self._probing.discard(profile_id)

    def remove(self, profile_id):
        self.resolve(profile_id)
        del self._profiles[profile_id]
