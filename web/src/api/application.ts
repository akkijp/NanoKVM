import { http } from '@/lib/http.ts';
import { getBaseUrl } from '@/lib/service.ts';

export type UpdateServerConfig = {
  enabled: boolean;
  url: string;
};

// get application version
// akkijp fork: local=true answers without asking GitHub (current version, start time, install count).
export function getVersion(local = false) {
  return http.get('/api/application/version', local ? { params: { local: 1 } } : undefined);
}

// update application to latest version
// akkijp fork: the version shown on the page; the server installs only that one.
export function update(version: string) {
  return http.request({
    method: 'post',
    url: '/api/application/update',
    data: { version },
    timeout: 15 * 60 * 1000
  });
}

// akkijp fork: restart into an installed update that is waiting for its restart (installs nothing)
export function retryRestart() {
  return http.request({
    method: 'post',
    url: '/api/application/update/restart'
  });
}

// offline update application
export function offlineUpdate(data: FormData, sha256Checksum = '') {
  const baseUrl = getBaseUrl('http');
  const url = `${baseUrl}/api/application/update/offline`;
  return fetch(url, {
    method: 'POST',
    headers: sha256Checksum ? { 'X-SHA256-Checksum': sha256Checksum } : undefined,
    body: data
  });
}

// enable/disable preview updates
export function setPreviewUpdates(enable: boolean) {
  const data = {
    enable
  };
  return http.post('/api/application/preview', data);
}

// get preview updates state
export function getPreviewUpdates() {
  return http.get('/api/application/preview');
}

// get custom update server configuration
export function getUpdateServer() {
  return http.get('/api/application/update-server');
}

// enable/disable custom update server
export function setUpdateServer(config: UpdateServerConfig) {
  return http.post('/api/application/update-server', config);
}
