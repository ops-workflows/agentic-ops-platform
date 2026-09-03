import type { NextRequest } from 'next/server';
import { proxyGatewayRequest } from '@/lib/gateway-proxy';

export const dynamic = 'force-dynamic';

type RouteContext = { params: { path?: string[] } };

function handler(request: NextRequest, { params }: RouteContext) {
  return proxyGatewayRequest(request, 'webhooks', params.path ?? []);
}

export {
  handler as DELETE,
  handler as GET,
  handler as HEAD,
  handler as OPTIONS,
  handler as PATCH,
  handler as POST,
  handler as PUT,
};
