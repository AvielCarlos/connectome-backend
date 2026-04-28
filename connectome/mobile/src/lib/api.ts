import axios from 'axios';
import { getToken } from './auth';

export const BASE_URL = 'https://connectome-api-production.up.railway.app';

export const api = axios.create({
  baseURL: BASE_URL,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

// Attach JWT on every request
api.interceptors.request.use(async (config) => {
  const token = await getToken();
  if (token) {
    config.headers = config.headers ?? {};
    config.headers['Authorization'] = `Bearer ${token}`;
  }
  return config;
});

// ── Auth ──────────────────────────────────────────────────────────────────────

export interface LoginResponse {
  token: string;
  user_id: string;
  email: string;
  name: string;
}

export async function login(email: string, password: string): Promise<LoginResponse> {
  const res = await api.post<LoginResponse>('/api/users/login', { email, password });
  return res.data;
}

export interface RegisterResponse {
  token: string;
  user_id: string;
  email: string;
  name: string;
}

export async function register(
  name: string,
  email: string,
  password: string,
): Promise<RegisterResponse> {
  const res = await api.post<RegisterResponse>('/api/users/register', { name, email, password });
  return res.data;
}

// ── Profile ───────────────────────────────────────────────────────────────────

export interface UserProfile {
  user_id: string;
  name: string;
  email: string;
  tier: string;
  goals?: any[];
  goals_count?: number;
  interactions_count?: number;
}

export async function getProfile(): Promise<UserProfile> {
  const res = await api.get<UserProfile>('/api/users/me');
  return res.data;
}

// ── Feed / Screen ─────────────────────────────────────────────────────────────

export interface ScreenCard {
  screen_id: string;
  title: string;
  description: string;
  category: string;
  content?: string;
}

export async function getScreen(userId: string): Promise<ScreenCard> {
  const res = await api.post<ScreenCard>('/api/ora/screen', {
    user_id: userId,
    variant: 'A',
  });
  return res.data;
}

// ── Interactions ──────────────────────────────────────────────────────────────

export interface InteractionPayload {
  screen_id: string;
  rating?: number;
  action: 'rate' | 'save' | 'skip';
}

export async function sendInteraction(payload: InteractionPayload): Promise<void> {
  await api.post('/api/interactions', payload);
}

// ── Ora Chat ──────────────────────────────────────────────────────────────────

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatResponse {
  message: string;
  response?: string;
}

export async function chatWithOra(
  message: string,
  conversation_history: ChatMessage[],
): Promise<string> {
  const res = await api.post<ChatResponse>('/api/ora/chat', {
    message,
    conversation_history,
  });
  return res.data.message ?? res.data.response ?? '';
}
