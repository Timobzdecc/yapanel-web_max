"""Доменные HTTP-маршруты панели, собранные тонким роутером."""
from .router import ApiRequest, Response, Router


def build_router():
    from . import dsp, extmusic, fs, media, party, phrases, yam
    router = Router()
    for module in (party, dsp, yam, media, fs, phrases, extmusic):
        module.register(router)
    return router


ROUTER = build_router()
