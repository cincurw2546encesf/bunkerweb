-- Space-Saving Top-N tracker (Metwally et al., PODS 2005).
-- Caller schedules :decay() periodically - without it the cached __min
-- drifts upward and admits low-frequency keys at inflated counts.

local class = require "middleclass"

local ngx = ngx
local shared = ngx.shared
local log = ngx.log
local ERR = ngx.ERR
local floor = math.floor
local min_fn = math.min
local sort = table.sort
local sub = string.sub
local gsub = string.gsub

local VALUE_MAX_BYTES = 256
local FORBIDDEN_VALUE_CHARS = "[%c%z]"

local top_n = class("top_n")

function top_n:initialize(dict_name, prefix, max_track, top_ranks)
	if not shared[dict_name] then
		error('shared dict "' .. tostring(dict_name) .. '" not defined', 2)
	end
	if not prefix or prefix == "" then
		error("top_n: prefix is required", 2)
	end
	self.dict = shared[dict_name]
	self.prefix = prefix
	self.max_track = max_track or 5000
	self.top_ranks = top_ranks or 50
	-- Internal state keys, namespaced under the instance prefix to keep
	-- multiple top_n instances on the same shdict isolated.
	self.count_key = prefix .. "__count"
	self.min_count_key = prefix .. "__min"
	self.min_key_key = prefix .. "__min_key"
end

local function is_internal(self, full_key)
	return full_key == self.count_key or full_key == self.min_count_key or full_key == self.min_key_key
end

local function sanitize_value(value)
	if value == nil then
		return nil
	end
	if type(value) ~= "string" then
		value = tostring(value)
	end
	if #value > VALUE_MAX_BYTES then
		value = sub(value, 1, VALUE_MAX_BYTES)
	end
	value = gsub(value, FORBIDDEN_VALUE_CHARS, "_")
	if value == "" then
		return nil
	end
	return value
end

function top_n:incr(value)
	value = sanitize_value(value)
	if not value then
		return
	end
	local key = self.prefix .. value
	if is_internal(self, key) then
		return
	end
	local new_val, err = self.dict:incr(key, 1)
	if new_val then
		return
	end
	if err and err ~= "not found" then
		log(ERR, "top_n: incr failed for prefix ", self.prefix, ": ", err)
		return
	end
	-- dict:add is the only race-safe create primitive: concurrent admit
	-- attempts on the same key resolve to one winner (ok=true) and one loser
	-- (err="exists"), so __count stays accurate.
	local count = self.dict:get(self.count_key) or 0
	if count < self.max_track then
		local ok, add_err, forcible = self.dict:add(key, 1)
		if ok then
			self.dict:incr(self.count_key, 1, 0)
			if forcible then
				log(ERR, "top_n: shdict ", tostring(self.prefix), " full, key evicted on add")
			end
			return
		end
		if add_err == "exists" then
			self.dict:incr(key, 1, 0)
			return
		end
		log(ERR, "top_n: add failed for prefix ", self.prefix, ": ", tostring(add_err))
		return
	end
	local min_count = self.dict:get(self.min_count_key)
	local min_key = self.dict:get(self.min_key_key)
	if not (min_count and min_key) then
		return
	end
	if min_key ~= key then
		self.dict:delete(min_key)
	end
	self.dict:set(key, min_count + 1)
	-- Cached min is approximate; :decay() repairs the drift periodically.
	self.dict:set(self.min_count_key, min_count + 1)
	self.dict:set(self.min_key_key, key)
end

function top_n:list()
	local all_keys = self.dict:get_keys(0)
	local rows = {}
	local plen = #self.prefix
	for _, k in ipairs(all_keys) do
		if sub(k, 1, plen) == self.prefix and not is_internal(self, k) then
			local count = self.dict:get(k)
			if count and count > 0 then
				rows[#rows + 1] = { value = sub(k, plen + 1), count = count }
			end
		end
	end
	sort(rows, function(a, b)
		return a.count > b.count
	end)
	local n = min_fn(#rows, self.top_ranks)
	local out = {}
	for i = 1, n do
		out[i] = rows[i]
	end
	return out
end

-- Run from a single worker; parallel decays race on the cached min.
function top_n:decay()
	local all_keys = self.dict:get_keys(0)
	local plen = #self.prefix
	local min_count, min_key
	local surviving = 0
	for _, k in ipairs(all_keys) do
		if sub(k, 1, plen) == self.prefix and not is_internal(self, k) then
			local count = self.dict:get(k)
			if count then
				local halved = floor(count / 2)
				if halved < 2 then
					self.dict:delete(k)
				else
					self.dict:set(k, halved)
					surviving = surviving + 1
					if not min_count or halved < min_count then
						min_count = halved
						min_key = k
					end
				end
			end
		end
	end
	self.dict:set(self.count_key, surviving)
	if min_count and min_key then
		self.dict:set(self.min_count_key, min_count)
		self.dict:set(self.min_key_key, min_key)
	else
		self.dict:delete(self.min_count_key)
		self.dict:delete(self.min_key_key)
	end
end

function top_n:clear()
	local all_keys = self.dict:get_keys(0)
	local plen = #self.prefix
	for _, k in ipairs(all_keys) do
		if sub(k, 1, plen) == self.prefix or is_internal(self, k) then
			self.dict:delete(k)
		end
	end
end

function top_n:schedule_decay(interval_seconds)
	if not interval_seconds or interval_seconds <= 0 then
		return "invalid interval"
	end
	local self_ref = self
	local ok, err = ngx.timer.every(interval_seconds, function(premature)
		if premature then
			return
		end
		local pcall_ok, pcall_err = pcall(function()
			self_ref:decay()
		end)
		if not pcall_ok then
			log(ERR, "top_n: decay timer failed for prefix ", self_ref.prefix, ": ", tostring(pcall_err))
		end
	end)
	if not ok then
		return err
	end
end

return top_n
