export const API_BASE_URL = 'https://api.example.com';

export class HttpError extends Error {
  status: number;

  constructor(status: number) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}

export type GetJson = (path: string) => Promise<unknown>;

export const getJson: GetJson = async (path) => {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new HttpError(response.status);
  }
  return response.json();
};
