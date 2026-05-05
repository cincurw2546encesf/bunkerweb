local cjson = require "cjson"
local class = require "middleclass"
local datastore = require "bunkerweb.datastore"
local lrucache = require "resty.lrucache"
local plugin = require "bunkerweb.plugin"
local utils = require "bunkerweb.utils"

local metrics = class("metrics", plugin)
local ngx = ngx
local ERR = ngx.ERR
local INFO = ngx.INFO
local null = ngx.null
local unescape_uri = ngx.unescape_uri

-- lrucache.new() allocates upfront and is not resizable, so the cap has to
-- be picked at module load before the internalstore variables map exists -
-- that's why we read variables.env directly below.
local LRU_DEFAULT = 10000
local LRU_MIN = 100
local LRU_MAX = 100000

local function resolve_lru_size()
	local size = LRU_DEFAULT
	local f = io.open("/etc/nginx/variables.env", "r")
	if f then
		for line in f:lines() do
			local k, v = line:match("^([^=]+)=(.*)$")
			if k == "METRICS_LRU_SIZE" then
				local n = tonumber(v)
				if n then
					size = n
				end
				break
			end
		end
		f:close()
	end
	if size < LRU_MIN then
		size = LRU_MIN
	elseif size > LRU_MAX then
		size = LRU_MAX
	end
	return size
end

local lru, err_lru = lrucache.new(resolve_lru_size())
if not lru then
	require "bunkerweb.logger":new("METRICS"):log(ERR, "failed to instantiate LRU cache : " .. err_lru)
end

-- Defense in depth against a future producer accidentally re-introducing a
-- per-key event log under tables/*.
local TABLE_VALUE_CAP = 100

local TOP_N_RANKS_API = 50

local shared = ngx.shared
local subsystem = ngx.config.subsystem
local HTTP_INTERNAL_SERVER_ERROR = ngx.HTTP_INTERNAL_SERVER_ERROR
local HTTP_OK = ngx.HTTP_OK
local worker = ngx.worker
local worker_id = worker.id

local get_reason = utils.get_reason
local get_country = utils.get_country
local has_variable = utils.has_variable
local is_connection_error = utils.is_connection_error
local encode = cjson.encode
local decode = cjson.decode

local match = string.match
local time = os.time
local tonumber = tonumber
local tostring = tostring
local table_insert = table.insert
local table_remove = table.remove

local REQUEST_FACET_FIELDS = { "ip", "country", "method", "url", "status", "reason", "server_name", "security_mode" }

local function get_request_facet_value(request, field)
	local value = request[field]
	if value == nil or value == "" or value == null then
		return "N/A"
	end
	return tostring(value)
end

local function clear_request_facets(self, set_initialized)
	if set_initialized == nil then
		set_initialized = true
	end

	for _, field in ipairs(REQUEST_FACET_FIELDS) do
		local _, err = self:redis_call("del", "requests:facet:" .. field)
		if err then
			self:log_throttled(ERR, "facet_clear", "Can't clear request facet " .. field .. ": " .. err)
		end
	end

	if set_initialized then
		local ok, err = self:redis_call("set", "requests:facets:initialized", "1")
		if not ok then
			self:log_throttled(
				ERR,
				"facet_init_set",
				"Can't mark request facets as initialized: " .. (err or "unknown error")
			)
		end
	end
end

local function incr_request_facets(self, request)
	for _, field in ipairs(REQUEST_FACET_FIELDS) do
		local facet_key = "requests:facet:" .. field
		local facet_value = get_request_facet_value(request, field)
		local count_raw, err = self:redis_call("hincrby", facet_key, facet_value, 1)
		if count_raw == nil or count_raw == false then
			self:log_throttled(
				ERR,
				"facet_incr",
				"Can't increment request facet " .. field .. "=" .. facet_value .. ": " .. (err or "unknown error")
			)
		end
	end
end

local function decr_request_facets(self, request)
	for _, field in ipairs(REQUEST_FACET_FIELDS) do
		local facet_key = "requests:facet:" .. field
		local facet_value = get_request_facet_value(request, field)
		local count_raw, err = self:redis_call("hincrby", facet_key, facet_value, -1)
		if count_raw == nil or count_raw == false then
			self:log_throttled(
				ERR,
				"facet_decr",
				"Can't decrement request facet " .. field .. "=" .. facet_value .. ": " .. (err or "unknown error")
			)
		else
			local count = tonumber(count_raw)
			if count and count <= 0 then
				local ok, hdel_err = self:redis_call("hdel", facet_key, facet_value)
				if ok == nil or ok == false then
					self:log_throttled(
						ERR,
						"facet_hdel",
						"Can't remove empty request facet "
							.. field
							.. "="
							.. facet_value
							.. ": "
							.. (hdel_err or "unknown error")
					)
				end
			end
		end
	end
end

local function initialize_request_facets(self)
	local initialized_raw, _ = self:redis_call("get", "requests:facets:initialized")
	if
		initialized_raw ~= nil
		and initialized_raw ~= false
		and initialized_raw ~= null
		and tostring(initialized_raw) == "1"
	then
		return
	end

	clear_request_facets(self, false)

	local nb_requests_raw, len_err = self:redis_call("llen", "requests")
	if nb_requests_raw == nil or nb_requests_raw == false then
		self:log_throttled(
			ERR,
			"facet_init_llen",
			"Can't initialize request facets, failed to get requests length: " .. (len_err or "unknown error")
		)
		return
	end

	local nb_requests = tonumber(nb_requests_raw) or 0
	if nb_requests > 0 then
		local chunk_size = 1000
		for start_idx = 0, nb_requests - 1, chunk_size do
			local stop_idx = start_idx + chunk_size - 1
			local chunk, range_err = self:redis_call("lrange", "requests", start_idx, stop_idx)
			if chunk == nil or chunk == false then
				self:log_throttled(
					ERR,
					"facet_init_lrange",
					"Can't initialize request facets, failed to read requests chunk: " .. (range_err or "unknown error")
				)
				break
			end
			for _, request_raw in ipairs(chunk) do
				if request_raw ~= nil and request_raw ~= false and request_raw ~= null then
					local ok, request = pcall(decode, request_raw)
					if ok and type(request) == "table" then
						incr_request_facets(self, request)
					end
				end
			end
		end
	end

	local ok, set_err = self:redis_call("set", "requests:facets:initialized", "1")
	if not ok then
		self:log_throttled(
			ERR,
			"facet_init_final",
			"Can't finalize request facets initialization: " .. (set_err or "unknown error")
		)
	end
end

local function enforce_redis_requests_cap(self)
	local max_requests = tonumber(self.variables["METRICS_MAX_BLOCKED_REQUESTS_REDIS"]) or 0
	if max_requests < 0 then
		max_requests = 0
	end

	local nb_requests_raw, err = self:redis_call("llen", "requests")
	if nb_requests_raw == nil or nb_requests_raw == false then
		self:log_throttled(ERR, "cap_llen", "Can't get Redis requests length: " .. (err or "unknown error"))
		return
	end

	local nb_requests = tonumber(nb_requests_raw)
	if not nb_requests then
		self:log_throttled(ERR, "cap_parse", "Can't parse Redis requests length: " .. tostring(nb_requests_raw))
		return
	end

	if nb_requests <= max_requests then
		return
	end

	local ok
	if max_requests == 0 then
		ok, err = self:redis_call("del", "requests")
		if ok then
			clear_request_facets(self)
		end
	else
		-- Decrement facets for the entries about to be evicted before we trim
		-- them - once LTRIM runs we lose the payloads needed to rebuild the
		-- correct facet counts. The LRANGE here is bounded by `to_remove`
		-- (computed from LLEN above) so it never reads more than we intend
		-- to trim, and is replaced by an O(1) LTRIM regardless of magnitude.
		local to_remove = nb_requests - max_requests
		local evicted, range_err = self:redis_call("lrange", "requests", 0, to_remove - 1)
		if evicted == nil or evicted == false then
			self:log_throttled(
				ERR,
				"cap_lrange",
				"Can't preview Redis requests trim window: " .. (range_err or "unknown error")
			)
		elseif evicted ~= null and type(evicted) == "table" then
			for _, removed_raw in ipairs(evicted) do
				if removed_raw ~= nil and removed_raw ~= false and removed_raw ~= null then
					local decoded_ok, removed_request = pcall(decode, removed_raw)
					if decoded_ok and type(removed_request) == "table" then
						decr_request_facets(self, removed_request)
					end
				end
			end
		end
		-- O(1) trim regardless of how far we are over cap. Replaces the prior
		-- per-entry LPOP loop that produced the customer's "LRANGE requests
		-- 0 …465999" / sustained-LPOP slowlog signature.
		ok, err = self:redis_call("ltrim", "requests", to_remove, -1)
	end

	if not ok then
		self:log_throttled(ERR, "cap_enforce", "Can't enforce Redis requests cap: " .. (err or "unknown error"))
	end
end

-- One-shot 1.6.9 -> 1.6.10 cleanup of unbounded per-IP/per-user event lists.
local LEGACY_TABLE_PATTERNS = {
	"metrics:*_table_increments_*",
	"metrics:*_table_authentications*",
}
local LEGACY_CLEANUP_DONE_KEY = "metrics:cleanup_v2_done"
local LEGACY_CLEANUP_LOCK_KEY = "metrics:cleanup_v2_in_progress"
-- Bounded by Redis command arg count and LuaJIT's stack on unpack().
local UNLINK_CHUNK_SIZE = 500
local function cleanup_legacy_table_keys(self)
	-- Already done? skip.
	local done = self:redis_call("get", LEGACY_CLEANUP_DONE_KEY)
	if done and done ~= ngx.null and done ~= false then
		return
	end
	-- Try to claim the lock. SET NX EX is atomic.
	local lock_ok = self:redis_call("set", LEGACY_CLEANUP_LOCK_KEY, "1", "NX", "EX", 3600)
	if not lock_ok or lock_ok == false or lock_ok == ngx.null then
		-- Another instance is running the cleanup, or Redis returned an
		-- error. Either way, don't proceed.
		return
	end
	local clean_completed = true
	for _, pattern in ipairs(LEGACY_TABLE_PATTERNS) do
		local cursor = "0"
		repeat
			local res, scan_err = self:redis_call("scan", cursor, "MATCH", pattern, "COUNT", 1000)
			if not res or type(res) ~= "table" then
				self:log_throttled(
					ERR,
					"cleanup_v2_scan",
					"Can't SCAN for legacy table keys (pattern=" .. pattern .. "): " .. (scan_err or "unknown error")
				)
				clean_completed = false
				break
			end
			cursor = res[1] or "0"
			local keys = res[2]
			if type(keys) == "table" and #keys > 0 then
				-- Chunked unlink to stay within LuaJIT's call-stack budget on
				-- real customer keyspaces that may carry tens of thousands of
				-- legacy keys per pattern.
				local total = #keys
				for i = 1, total, UNLINK_CHUNK_SIZE do
					local last = i + UNLINK_CHUNK_SIZE - 1
					if last > total then
						last = total
					end
					local ok, unlink_err = self:redis_call("unlink", unpack(keys, i, last))
					if not ok then
						self:log_throttled(
							ERR,
							"cleanup_v2_unlink",
							"Can't UNLINK legacy table keys: " .. (unlink_err or "unknown error")
						)
						clean_completed = false
						break
					end
				end
				if not clean_completed then
					break
				end
			end
		until cursor == "0"
		if not clean_completed then
			break
		end
	end
	if clean_completed then
		-- Promote: mark done permanently (30 days), drop the lock.
		self:redis_call("set", LEGACY_CLEANUP_DONE_KEY, "1", "EX", 86400 * 30)
		self:redis_call("del", LEGACY_CLEANUP_LOCK_KEY)
		self.logger:log(INFO, "[METRICS] legacy 1.6.9 table-key migration completed")
	end
	-- On failure: leave the lock TTL to expire so another retry can pick up.
end

function metrics:initialize(ctx)
	-- Call parent initialize
	plugin.initialize(self, "metrics", ctx)
	local dict
	if subsystem == "http" then
		dict = shared.metrics_datastore
	else
		dict = shared.metrics_datastore_stream
	end
	self.metrics_datastore = datastore:new(dict)
end

-- Call Redis with one automatic reconnect attempt on connection error.
-- Must be called after self.clusterstore:connect() has succeeded.
-- Acts as a circuit-breaker: once self.redis_ok is false, all calls
-- are short-circuited for the rest of the timer cycle.
function metrics:redis_call(method, ...)
	if self.redis_ok == false then
		return false, "Redis unavailable for this cycle"
	end
	local res, call_err = self.clusterstore:call(method, ...)
	if not res and call_err and is_connection_error(call_err) then
		self.clusterstore:close()
		local ok, reconnect_err = self.clusterstore:connect()
		if not ok then
			self:log_throttled(
				ERR,
				"redis_reconnect",
				"Can't reconnect to Redis: " .. (reconnect_err or "unknown error")
			)
			self.redis_ok = false
			return false, call_err
		end
		local res2, err2 = self.clusterstore:call(method, ...)
		if not res2 and err2 then
			self.redis_ok = false
		end
		return res2, err2
	end
	return res, call_err
end

function metrics:log(bypass_checks)
	-- Don't go further if metrics is not enabled
	if not bypass_checks and self.variables["USE_METRICS"] == "no" then
		return self:ret(true, "metrics are disabled")
	end
	-- Store blocked requests
	local reason, data, security_mode = get_reason(self.ctx)
	if reason then
		local country = "local"
		local err
		if self.ctx.bw.ip_is_global then
			country, err = get_country(self.ctx.bw.remote_addr)
			if not country then
				country = "unknown"
				self.logger:log(ERR, "can't get country code " .. err)
			end
		end
		local request = {
			id = self.ctx.bw.request_id,
			date = self.ctx.bw.start_time or time(),
			ip = self.ctx.bw.remote_addr,
			country = country,
			method = self.ctx.bw.request_method,
			url = self.ctx.bw.request_uri,
			status = ngx.status,
			user_agent = self.ctx.bw.http_user_agent or "",
			reason = reason,
			server_name = self.ctx.bw.server_name,
			data = data,
			security_mode = security_mode,
			synced = not self.use_redis,
		}
		-- Get requests from LRU
		local requests = lru:get("requests") or {}

		-- Add to LRU
		table_insert(requests, request)

		-- Remove old requests if needed
		local max_requests = tonumber(self.variables["METRICS_MAX_BLOCKED_REQUESTS"])
		while #requests > max_requests do
			table_remove(requests, 1)
		end

		-- Update worker cache
		lru:set("requests", requests)
	end
	-- Get metrics from plugins
	local all_metrics = self.ctx.bw.metrics
	if all_metrics then
		-- Loop on plugins
		for plugin_id, plugin_metrics in pairs(all_metrics) do
			-- Loop on kinds
			for kind, kind_metrics in pairs(plugin_metrics) do
				-- Increment counters
				if kind == "counters" then
					for metric_key, metric_value in pairs(kind_metrics) do
						local lru_key = plugin_id .. "_counter_" .. metric_key
						local metric_counter = lru:get(lru_key)
						if not metric_counter then
							metric_counter = metric_value
						else
							metric_counter = metric_counter + metric_value
						end
						lru:set(lru_key, metric_counter)
					end
				-- Add table entries
				elseif kind == "tables" then
					for metric_key, metric_value in pairs(kind_metrics) do
						local lru_key = plugin_id .. "_table_" .. metric_key
						local metric_table = lru:get(lru_key) or {}
						-- Add value to table
						table_insert(metric_table, metric_value)
						-- Bound the per-key array. Without this cap a misbehaving
						-- producer (or a high-rate attacker) can grow individual
						-- entries without bound between timer flushes.
						while #metric_table > TABLE_VALUE_CAP do
							table_remove(metric_table, 1)
						end
						-- Update LRU cache
						lru:set(lru_key, metric_table)
					end
				end
			end
		end
	end
	return self:ret(true, "success")
end

function metrics:log_default()
	local is_needed, err = has_variable("USE_METRICS", "yes")
	if is_needed == nil then
		return self:ret(false, "can't check USE_METRICS variable : " .. err)
	end
	if is_needed then
		return self:log(true)
	end
	return self:ret(true, "metrics not used")
end

function metrics:timer()
	-- Check if metrics is used
	local is_needed, err = has_variable("USE_METRICS", "yes")
	if is_needed == nil then
		return self:ret(false, "can't check USE_METRICS variable : " .. err)
	end
	if not is_needed then
		return self:ret(true, "metrics not used")
	end

	local ret = true
	local ret_err = "metrics updated"
	local wid = tostring(worker_id())

	-- Purpose of following code is to populate the LRU cache.
	-- In case of a reload, everything in LRU cache is removed
	-- so we need to copy it from SHM cache if it exists.
	local setup = lru:get("setup")
	if not setup then
		for _, key in ipairs(self.metrics_datastore:keys()) do
			if key:match("_" .. wid .. "$") then
				local value
				value, err = self.metrics_datastore:get(key)
				if not value and err ~= "not found" then
					ret = false
					ret_err = err
					self.logger:log(ERR, "error while checking " .. key .. " : " .. err)
				end
				if value then
					local ok, decoded = pcall(decode, value)
					if ok then
						value = decoded
					end
					-- Drop legacy table-style entries that 1.6.9 wrote: they
					-- were per-IP/per-user unbounded event arrays. New code
					-- replaces them with bounded Top-N gauges; keeping the
					-- old values in the LRU would re-pollute the SHM dict on
					-- the next sync.
					local local_key = key:gsub("_" .. wid .. "$", "")
					if not local_key:match("_table_increments_") and not local_key:match("_table_authentications") then
						lru:set(local_key, value)
					else
						-- Surgical purge from SHM too.
						self.metrics_datastore:delete(key)
					end
				end
			end
		end
		lru:set("setup", true)
	end

	self.redis_ok = nil
	if self.use_redis then
		self.redis_ok, err = self.clusterstore:connect()
		if not self.redis_ok then
			self:log_throttled(
				ERR,
				"redis_connect",
				"Can't connect to Redis server: "
					.. (err or "unknown error")
					.. " - requests will be stored in datastore"
			)
		else
			initialize_request_facets(self)
			-- One-shot upgrade migration: clean up the legacy unbounded
			-- per-IP / per-user event lists from 1.6.9 (badbehavior wrote
			-- `metrics:badbehavior_table_increments_<ip>:<wid>`, authbasic
			-- wrote `metrics:authbasic_table_authentications:<wid>`). Gated
			-- on a SETNX flag so this runs at most once cluster-wide. Uses
			-- SCAN+UNLINK so a multi-million-key keyspace doesn't block the
			-- Redis event loop.
			cleanup_legacy_table_keys(self)
		end
	end

	-- Loop on all keys
	for _, key in ipairs(lru:get_keys()) do
		-- Get LRU data
		local value = lru:get(key)
		if self.redis_ok then
			if key == "requests" then
				for _, request in ipairs(value) do
					if not request.synced then
						-- Add only unsynced requests
						local ok
						ok, err = self:redis_call("rpush", "requests", encode(request))
						if not ok then
							self:log_throttled(ERR, "sync_request", "Can't sync request to Redis: " .. err)
							break
						end
						request.synced = true -- Mark as synced
						incr_request_facets(self, request)
					end
				end

				-- Update LRU cache
				lru:set("requests", value)
			elseif key ~= "setup" and self.variables["METRICS_SAVE_TO_REDIS"] == "yes" then
				-- Sync other metrics (counters and tables) to Redis with optimized data structures
				local redis_key = "metrics:" .. key .. ":" .. wid
				local ok
				if type(value) == "table" then
					-- Use Redis list for table values
					ok, err = self:redis_call("del", redis_key)
					if ok then
						for _, item in ipairs(value) do
							local item_value = type(item) == "table" and encode(item) or tostring(item)
							ok, err = self:redis_call("rpush", redis_key, item_value)
							if not ok then
								self:log_throttled(
									ERR,
									"sync_table_item",
									"Can't push metric table item " .. key .. " to Redis: " .. err
								)
								break
							end
						end
					else
						self:log_throttled(
							ERR,
							"sync_table_clear",
							"Can't clear metric table " .. key .. " in Redis: " .. err
						)
					end
				elseif type(value) == "number" then
					-- Use Redis string for numeric counters
					ok, err = self:redis_call("set", redis_key, value)
					if not ok then
						self:log_throttled(
							ERR,
							"sync_counter",
							"Can't sync metric counter " .. key .. " to Redis: " .. err
						)
					end
				else
					-- Use Redis string for other types
					ok, err = self:redis_call("set", redis_key, tostring(value))
					if not ok then
						self:log_throttled(ERR, "sync_other", "Can't sync metric " .. key .. " to Redis: " .. err)
					end
				end
			end
		end
		if type(value) == "table" then
			value = encode(value)
		end
		-- Push to dict (with LRU eviction if needed)
		local ok
		ok, err = self.metrics_datastore:set_with_retries(key .. "_" .. wid, value)
		if not ok then
			-- If there isn't enough memory : we fallback to delete everything
			if err == "no memory" then
				self.logger:log(INFO, "not enough memory in the metrics datastore, purging LRU key " .. key)
				lru:delete(key)
			else
				ret = false
				ret_err = err
				self:log_throttled(ERR, "datastore_set", "can't set " .. key .. "_" .. wid .. " : " .. err)
			end
		end
	end

	if self.redis_ok then
		enforce_redis_requests_cap(self)
	end
	-- Always attempt cleanup when Redis was used, even if connection dropped mid-cycle.
	-- clusterstore:close() handles the "client is not instantiated" case gracefully.
	if self.use_redis then
		self.clusterstore:close()
	end

	-- Flush any end-of-window recaps for errors that stopped repeating.
	self:flush_log_recaps()

	-- Done
	return self:ret(ret, ret_err)
end

local function escape_lua_pattern(s)
	return (s:gsub("([%(%)%.%%%+%-%*%?%[%]%^%$])", "%%%1"))
end

-- Must stay in sync with bunkerweb.top_n's internal state keys.
local TOPN_BOOKKEEPING_SUFFIXES = { "__count", "__min", "__min_key" }
local function is_topn_bookkeeping_key(key)
	for _, suffix in ipairs(TOPN_BOOKKEEPING_SUFFIXES) do
		local slen = #suffix
		if #key >= slen and key:sub(-slen) == suffix then
			return true
		end
	end
	return false
end

function metrics:api()
	-- Match request
	if not match(self.ctx.bw.uri, "^/metrics/.+$") or self.ctx.bw.request_method ~= "GET" then
		return self:ret(false, "success")
	end
	-- Extract filter parameter
	local filter = self.ctx.bw.uri:gsub("^/metrics/", "")

	-- Handle special /metrics/requests/query endpoint for optimized queries
	if filter == "requests/query" then
		return self:api_requests_query()
	end

	if not filter:match("^[%w_]+$") then
		return self:ret(false, "invalid metrics filter")
	end
	local filter_pat = escape_lua_pattern(filter)
	-- Prefix-stripping (not regex) so IPv6 values with embedded colons round-trip cleanly.
	local topn_prefix = filter .. "_topn_"
	local topn_prefix_len = #topn_prefix

	local metrics_data = {}
	local topn_buckets = {}
	for _, key in ipairs(self.metrics_datastore:keys()) do
		if key:match("^" .. filter_pat .. "_") and not is_topn_bookkeeping_key(key) then
			if #key > topn_prefix_len and key:sub(1, topn_prefix_len) == topn_prefix then
				local rest = key:sub(topn_prefix_len + 1)
				local colon = rest:find(":", 1, true)
				if colon and colon > 1 and colon < #rest then
					local dim = rest:sub(1, colon - 1)
					local value = rest:sub(colon + 1)
					local count = self.metrics_datastore:get(key)
					if type(count) == "number" and count > 0 then
						if not topn_buckets[dim] then
							topn_buckets[dim] = {}
						end
						table_insert(topn_buckets[dim], { value = value, count = count })
					end
				end
			else
				local data, err = self.metrics_datastore:get(key)
				if not data then
					return self:ret(true, "error while fetching metric : " .. err, HTTP_INTERNAL_SERVER_ERROR)
				end
				local metric_key = key:gsub("_[0-9]+$", ""):gsub("^" .. filter_pat .. "_", "")
				if metric_key == "" then
					metric_key = filter
				end
				local ok, decoded = pcall(decode, data)
				if ok then
					data = decoded
				end
				if type(data) == "table" then
					if not metrics_data[metric_key] then
						metrics_data[metric_key] = {}
					end
					for _, metric_value in ipairs(data) do
						table_insert(metrics_data[metric_key], metric_value)
					end
				else
					-- Counter case
					if not metrics_data[metric_key] then
						metrics_data[metric_key] = 0
					end
					metrics_data[metric_key] = metrics_data[metric_key] + data
				end
			end
		end
	end
	-- Finalise Top-N buckets: sort each dim by count desc, cap at TOP_N_RANKS.
	for dim, rows in pairs(topn_buckets) do
		table.sort(rows, function(a, b)
			return a.count > b.count
		end)
		local capped = {}
		local cap = TOP_N_RANKS_API
		if #rows < cap then
			cap = #rows
		end
		for i = 1, cap do
			capped[i] = rows[i]
		end
		metrics_data["topn_" .. dim] = capped
	end
	return self:ret(true, metrics_data, HTTP_OK)
end

function metrics:api_requests_query()
	-- Parse query parameters from request args
	local args = ngx.req.get_uri_args()
	local start_idx = tonumber(args.start) or 0
	local length = tonumber(args.length) or 10
	local search = unescape_uri(args.search or "")
	local order_column = args.order_column or "date"
	local order_dir = args.order_dir or "desc"
	local count_only = args.count_only == "true"

	-- Parse search panes filters (format: field1:value1,value2;field2:value3)
	local search_panes = {}
	local search_panes_raw = unescape_uri(args.search_panes or "")
	if search_panes_raw and search_panes_raw ~= "" then
		for field_filter in search_panes_raw:gmatch("[^;]+") do
			local field, values = field_filter:match("^([^:]+):(.+)$")
			if field and values then
				search_panes[field] = {}
				for value in values:gmatch("[^,]+") do
					table_insert(search_panes[field], value)
				end
			end
		end
	end

	-- Collect all requests from all workers
	local all_requests = {}
	for _, key in ipairs(self.metrics_datastore:keys()) do
		if key:match("^requests_[0-9]+$") then
			local data, _ = self.metrics_datastore:get(key)
			if data then
				local ok, decoded = pcall(decode, data)
				if ok and type(decoded) == "table" then
					for _, request in ipairs(decoded) do
						table_insert(all_requests, request)
					end
				end
			end
		end
	end

	-- Filter requests
	local filtered_requests = {}
	for _, request in ipairs(all_requests) do
		-- Filter: status 400-499 or detect mode
		if (request.status and request.status >= 400 and request.status < 500) or request.security_mode == "detect" then
			local matches = true

			-- Apply search filter
			if search ~= "" then
				local search_lower = search:lower()
				matches = false
				for _, value in pairs(request) do
					if type(value) == "string" and value:lower():find(search_lower, 1, true) then
						matches = true
						break
					elseif type(value) == "number" and tostring(value):find(search_lower, 1, true) then
						matches = true
						break
					end
				end
			end

			-- Apply search panes filters
			if matches then
				for field, allowed_values in pairs(search_panes) do
					local field_value = tostring(request[field] or "N/A")
					local field_matches = false
					for _, allowed in ipairs(allowed_values) do
						if field_value == allowed then
							field_matches = true
							break
						end
					end
					if not field_matches then
						matches = false
						break
					end
				end
			end

			if matches then
				table_insert(filtered_requests, request)
			end
		end
	end

	-- If only count is requested, return early
	if count_only then
		return self:ret(true, { total = #all_requests, filtered = #filtered_requests }, HTTP_OK)
	end

	-- Sort filtered requests
	if order_column == "date" then
		table.sort(filtered_requests, function(a, b)
			local a_val = tonumber(a.date) or 0
			local b_val = tonumber(b.date) or 0
			if order_dir == "desc" then
				return a_val > b_val
			else
				return a_val < b_val
			end
		end)
	else
		table.sort(filtered_requests, function(a, b)
			local a_val = a[order_column] or ""
			local b_val = b[order_column] or ""
			if order_dir == "desc" then
				return a_val > b_val
			else
				return a_val < b_val
			end
		end)
	end

	-- Paginate
	local paginated = {}
	local end_idx = start_idx + length
	if length == -1 then
		end_idx = #filtered_requests
	end

	for i = start_idx + 1, math.min(end_idx, #filtered_requests) do
		table_insert(paginated, filtered_requests[i])
	end

	-- Calculate search panes options
	local pane_counts = {}
	local filtered_ids = {}
	for _, req in ipairs(filtered_requests) do
		filtered_ids[req.id] = true
	end

	local pane_fields = { "ip", "country", "method", "url", "status", "reason", "server_name", "security_mode" }
	for _, field in ipairs(pane_fields) do
		pane_counts[field] = {}
	end

	for _, request in ipairs(all_requests) do
		if (request.status and request.status >= 400 and request.status < 500) or request.security_mode == "detect" then
			for _, field in ipairs(pane_fields) do
				local value = tostring(request[field] or "N/A")
				if not pane_counts[field][value] then
					pane_counts[field][value] = { total = 0, count = 0 }
				end
				pane_counts[field][value].total = pane_counts[field][value].total + 1
				if filtered_ids[request.id] then
					pane_counts[field][value].count = pane_counts[field][value].count + 1
				end
			end
		end
	end

	return self:ret(true, {
		total = #all_requests,
		filtered = #filtered_requests,
		data = paginated,
		pane_counts = pane_counts,
	}, HTTP_OK)
end

return metrics
