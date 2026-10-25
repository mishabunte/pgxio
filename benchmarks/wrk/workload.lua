-- Tokens live in a generated mode-0600 file, never in process arguments/output.
local cfg = dofile(assert(os.getenv('PGXBENCH_WRK_CONFIG'), 'missing private wrk config'))
local threads = {}
local next_id = 0
statuses = {}

function setup(thread)
    thread:set('thread_id', next_id)
    next_id = next_id + 1
    table.insert(threads, thread)
end

local requests = {}
local counter = 0
function init(args)
    counter = (thread_id or 0) * 131
    for tenant, token in ipairs(cfg.tokens) do
        local headers = {['Content-Type']='application/json'}
        if cfg.auth_mode~='none' then headers['Authorization']='Bearer '..token end
        if cfg.api_key then headers['apikey']=cfg.api_key end
        requests[tenant] = {}
        for _, operation in ipairs({'bench_one','bench_page','bench_write'}) do
            local body = operation=='bench_page' and '{}' or '{"id":1}'
            local actual_operation=operation
            if cfg.public_fixture then
                body=body:sub(1,-2)..(body=='{}' and '' or ',')..'"tenant":"'..cfg.subjects[tenant]..'"}'
                actual_operation=operation:gsub('bench_','bench_public_')
            end
            if (cfg.request_bytes or 0)>0 then
                local remaining=cfg.request_bytes-#body-13
                assert(remaining>=0, 'requested input size too small')
                body=body:sub(1,-2)..',"padding":"'..string.rep('x',remaining)..'"}'
                assert(#body==cfg.request_bytes, 'input size mismatch')
            end
            if cfg.native then body='{"input":'..body..'}' end
            requests[tenant][operation]=wrk.format('POST',cfg.prefix..actual_operation,headers,body)
        end
    end
end

function request()
    counter=counter+1
    local tenant=(math.floor(counter/10)%#cfg.tokens)+1
    local operation=({one='bench_one',page='bench_page',write='bench_write',mixed='bench_page'})[cfg.workload]
    if cfg.workload=='mixed' and counter%10==0 then operation='bench_write' end
    return requests[tenant][operation]
end

function response(status, headers, body)
    local key='status_'..status
    _G[key]=(_G[key] or 0)+1
end

function done(summary, latency, requests_per_second)
    local totals={}
    for _, thread in ipairs(threads) do
        -- wrk2's old cross-state table copier reverses keys and values.
        -- Scalar globals work in both engines, with no per-request JSON work.
        for status=100,599 do
            local count=thread:get('status_'..status)
            if count then totals[status]=(totals[status] or 0)+count end
        end
    end
    local codes={}
    for status,count in pairs(totals) do table.insert(codes,string.format('"%s":%d',status,count)) end
    table.sort(codes)
    local errors=summary.errors
    local function number(n) if n~=n or n==math.huge or n==-math.huge then return 'null' end return string.format('%.6f',n) end
    io.write('PGXBENCH_JSON '..string.format(
        '{"requests":%d,"duration_seconds":%s,"bytes":%d,"statuses":{%s},'..
        '"errors":{"connect":%d,"read":%d,"write":%d,"status":%d,"timeout":%d},'..
        '"latency_ms":{"count":%d,"p50_ms":%s,"p95_ms":%s,"p99_ms":%s,"max_ms":%s}}\n',
        summary.requests,number(summary.duration/1000000),summary.bytes,table.concat(codes,','),
        errors.connect,errors.read,errors.write,errors.status,errors.timeout,#latency,
        number(latency:percentile(50)/1000),number(latency:percentile(95)/1000),
        number(latency:percentile(99)/1000),number(latency.max/1000)))
end
