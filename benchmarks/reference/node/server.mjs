// Benchmark reference, not a supported application framework or pgxio replacement.
import cluster from 'node:cluster';
import { readFileSync } from 'node:fs';
import { verify, createPublicKey } from 'node:crypto';
import Fastify from 'fastify';
import pg from 'pg';
import Ajv from 'ajv';
import { WebSocketServer } from 'ws';

const workers = Number(process.env.WORKERS || 1);
if (cluster.isPrimary) {
  for (let i = 0; i < workers; i++) cluster.fork();
  // An unexpected worker exit fails the benchmark rather than silently healing it.
  cluster.on('exit', () => process.exit(1));
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
    for (const worker of Object.values(cluster.workers)) worker.kill();
    process.exit(0);
  });
} else {
  await serve();
}

async function serve() {
  const manifest = JSON.parse(readFileSync('/bench/contract.json'));
  const ajv = new Ajv({strict: false, allErrors: false});
  const endpoints = new Map(manifest.endpoints.map(e => [e.name, {
    ...e, inputCheck: ajv.compile(e.input), outputCheck: ajv.compile(e.output),
  }]));
  const key = createPublicKey(readFileSync('/bench/jwt-public.pem'));
  const max = Number(process.env.POOL_SIZE || 4);
  const pool = new pg.Pool({connectionString: process.env.DATABASE_URL, max,
    idleTimeoutMillis: 0, connectionTimeoutMillis: 6000});
  const clients = new Set();
  let listenerReady = false;
  let active = 0;
  const queue = [];

  function authenticate(token) {
    if (typeof token !== 'string' || token.length > 8192) throw Error('auth');
    const pieces = token.split('.');
    if (pieces.length !== 3) throw Error('auth');
    const header = JSON.parse(Buffer.from(pieces[0], 'base64url'));
    const claims = JSON.parse(Buffer.from(pieces[1], 'base64url'));
    if (header.alg !== 'RS256' || header.kid !== 'pgxbench' ||
        !verify('RSA-SHA256', Buffer.from(pieces[0] + '.' + pieces[1]), key, Buffer.from(pieces[2], 'base64url')) ||
        claims.iss !== 'pgxbench' || !(claims.aud === 'pgxbench' || (Array.isArray(claims.aud) && claims.aud.includes('pgxbench'))) ||
        !Number.isInteger(claims.exp) || claims.exp <= Date.now()/1000 ||
        typeof claims.sub !== 'string' || !claims.sub.length || claims.sub.length > 256 || claims.sub.includes('\0')) {
      throw Error('auth');
    }
    return claims;
  }

  function dispatch() {
    while (active < max && queue.length) {
      const {fn, resolve, reject} = queue.shift();
      active++;
      Promise.resolve().then(fn).then(resolve, reject).finally(() => {active--; dispatch();});
    }
  }
  function schedule(fn, wait) {
    if (!wait && active >= max) return Promise.reject(Error('busy'));
    return new Promise((resolve, reject) => {queue.push({fn, resolve, reject}); dispatch();});
  }
  async function transaction(claims, name, input, check, readOnly, queued = false) {
    return schedule(async () => {
      const connection = await pool.connect();
      let failed = false;
      try {
        await connection.query(readOnly ? 'BEGIN READ ONLY' : 'BEGIN');
        await connection.query(`SELECT jsonb_build_object('claims',set_config('request.jwt.claims',$1,true),
          'timeout',set_config('statement_timeout','4000',true),
          'lock',set_config('lock_timeout','1000',true),'path',set_config('search_path','pg_catalog,web',true))`,
          [JSON.stringify({sub: claims.sub, role: 'web'})]);
        const response = await connection.query(`SELECT web.${name}($1::jsonb) AS value`, [JSON.stringify(input)]);
        const value = response.rows[0].value;
        if (Buffer.byteLength(JSON.stringify(value)) > 1048576 || (check && !check(value)) || claims.exp <= Date.now()/1000) {
          throw Error('result');
        }
        await connection.query('COMMIT');
        return value;
      } catch (error) {
        failed = true;
        throw error;
      } finally {
        // Closing an errored connection abandons the transaction, like pgxio.
        connection.release(failed);
      }
    }, queued);
  }
  async function reauthorize(state, id, sub, initial = false) {
    if (sub.pending || state.ws.readyState !== 1) return;
    sub.pending = true;
    sub.dirty = false;
    try {
      const route = await transaction(state.claims, 'bench_authorize', {}, null, true, true);
      if (!listenerReady || typeof route !== 'string' || (sub.key && sub.key !== route)) throw Error('denied');
      sub.key = route;
      state.ws.send(JSON.stringify({type: initial ? 'subscribed' : 'changed', id}));
    } catch {
      state.ws.close(1008);
    } finally {
      sub.pending = false;
      if (sub.dirty) reauthorize(state, id, sub);
    }
  }
  async function listen() {
    const listener = new pg.Client({connectionString: process.env.DATABASE_URL});
    const lost = () => {
      listenerReady = false;
      for (const state of clients) state.ws.close(1012);
      setTimeout(() => listen().catch(() => {}), 1000).unref();
    };
    listener.once('error', lost);
    try {
      await listener.connect();
      await listener.query('LISTEN pgxio_events');
      listenerReady = true;
      listener.on('notification', message => {
        for (const state of clients) for (const [id, sub] of state.subscriptions) {
          if (sub.key === message.payload) {
            sub.dirty = true;
            reauthorize(state, id, sub);
          }
        }
      });
    } catch {
      listener.removeAllListeners('error');
      await listener.end().catch(() => {});
      lost();
    }
  }
  await listen();
  function build(tls) {
    const app = Fastify({logger: false, bodyLimit: 65536,
      ...(tls ? {https: {key: readFileSync('/bench/tls-key.pem'), cert: readFileSync('/bench/tls-cert.pem'),
                        minVersion: 'TLSv1.3', maxVersion: 'TLSv1.3'}} : {})});
    app.get('/readyz', async (_, reply) => {
      if (!listenerReady || active >= max) return reply.code(503).send({status: 'unavailable'});
      return {status: 'ready'};
    });
    app.post('/v1/:name', async (request, reply) => {
      const endpoint = endpoints.get(request.params.name);
      if (!endpoint) return reply.code(404).send({error: 'not_found'});
      let claims;
      try {claims = authenticate(request.headers.authorization?.startsWith('Bearer ') ? request.headers.authorization.slice(7) : null);}
      catch {return reply.code(401).send({error: 'unauthorized'});}
      if (!endpoint.inputCheck(request.body)) return reply.code(400).send({error: 'invalid_input'});
      try {
        return await transaction(claims, endpoint.name, request.body, endpoint.outputCheck, endpoint.read_only);
      } catch {return reply.code(503).send({error: 'database_error'});}
    });
    const wss = new WebSocketServer({noServer: true, maxPayload: 16384,
      handleProtocols: protocols => protocols.has('pgxio.v1') ? 'pgxio.v1' : false});
    app.server.on('upgrade', (request, socket, head) => {
      if (request.url !== '/v1/events' || !listenerReady || clients.size >= Number(process.env.MAX_WEBSOCKETS || 1024) ||
          !request.headers['sec-websocket-protocol']?.split(',').map(s => s.trim()).includes('pgxio.v1')) {
        socket.end('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n');
        return;
      }
      wss.handleUpgrade(request, socket, head, ws => {
        const state = {ws, claims: null, subscriptions: new Map(), created: Date.now(), lastPing: Date.now(), pongDue: null};
        clients.add(state);
        ws.on('error', () => {});
        ws.on('close', () => clients.delete(state));
        ws.on('pong', () => {state.pongDue = null;});
        ws.on('message', raw => {
          try {
            const message = JSON.parse(raw);
            if (!state.claims) {
              if (message.type !== 'auth') throw Error('auth');
              state.claims = authenticate(message.token);
              ws.send(JSON.stringify({type: 'ready'}));
            } else if (message.type === 'subscribe') {
              if (typeof message.id !== 'string' || !message.id.length || message.id.length > 64 ||
                  message.stream !== 'bench' || Object.keys(message.params || {}).length ||
                  state.subscriptions.has(message.id) || state.subscriptions.size >= 16) throw Error('subscription');
              const sub = {key: null, pending: false, dirty: false};
              state.subscriptions.set(message.id, sub);
              reauthorize(state, message.id, sub, true);
            } else if (message.type === 'unsubscribe') {
              if (state.subscriptions.get(message.id)?.pending) throw Error('pending');
              state.subscriptions.delete(message.id);
            } else throw Error('protocol');
          } catch {ws.close(1008);}
        });
      });
    });
    return app;
  }
  setInterval(() => {
    for (const state of clients) {
      if ((!state.claims && Date.now() - state.created > 10000) ||
          (state.claims && state.claims.exp <= Date.now()/1000) || state.ws.bufferedAmount > 65536 ||
          (state.pongDue && Date.now() - state.pongDue > 10000)) state.ws.close(1008);
      else if (Date.now() - state.lastPing > 30000) {
        state.lastPing = Date.now(); state.pongDue = Date.now(); state.ws.ping();
      }
    }
  }, 1000).unref();
  await build(false).listen({host: '0.0.0.0', port: 8080});
  if (process.env.FRONTEND_TLS !== '0') await build(true).listen({host: '0.0.0.0', port: 8443});
}
