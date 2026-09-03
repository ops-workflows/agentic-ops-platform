import type { NextRequest } from 'next/server';

const DEFAULT_GATEWAY_URL = 'http://gateway:8080';
const BODYLESS_METHODS = new Set(['GET', 'HEAD']);

export async function proxyGatewayRequest(
  request: NextRequest,
  prefix: 'api' | 'webhooks',
  path: string[],
): Promise<Response> {
  const gatewayUrl = process.env.INTERNAL_GATEWAY_URL || DEFAULT_GATEWAY_URL;
  const upstreamUrl = new URL(
    `/${prefix}/${path.map(encodeURIComponent).join('/')}`,
    gatewayUrl,
  );
  upstreamUrl.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('content-length');

  return fetch(upstreamUrl, {
    method: request.method,
    headers,
    body: BODYLESS_METHODS.has(request.method)
      ? undefined
      : await request.arrayBuffer(),
    redirect: 'manual',
    cache: 'no-store',
  });
}
