// Station Max renderer profile (hardware capabilities stay in /api/profile).
export const MODEL_NAMES = { max: 'Станция Макс' };

export function rendererProfile(profile = {}, status = {}) {
  const hardware = String(status.hardware_model || '').toLowerCase();
  const hostname = String(status.hostname || '').trim().toLowerCase();
  const isMaxHardware =
    /(?:^|[^a-z0-9])(yandexstation_2|yandexstation|station[ -]?max)(?:[^a-z0-9]|$)/.test(hardware);
  const isMaxHost = /^yandex-station-max(?:-|$)/.test(hostname);
  const id = (isMaxHardware || isMaxHost || !profile.id || profile.id === 'max') ? 'max' : profile.id;
  return { ...profile, id, name: MODEL_NAMES[id] || profile.name || MODEL_NAMES.max };
}
