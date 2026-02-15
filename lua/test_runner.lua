-- WoW Lua Environment
_G.floor = math.floor
_G.min = math.min
_G.max = math.max
_G.gmatch = string.gmatch
_G.strsub = string.sub
_G.sort = table.sort
_G.tinsert = table.insert
_G.wipe = function(t) for i in pairs(t) do t[i] = nil end end

local AddonTable = {}
assert(loadfile("lua/algorithm.lua"))("", AddonTable)


local data_path = arg[1]
local f = loadfile(data_path)
if not f then
    io.stderr:write("Failed to load " .. data_path .. "\n")
    os.exit(1)
end
local data = f()
AddonTable.BonusIdAlgorithm.Initialize(data)

for line in io.lines() do
    local link, baseItemLevel, hasMidnightScaling = line:match("^(.-)\t(.-)\t(.-)$")
    baseItemLevel = tonumber(baseItemLevel)
    hasMidnightScaling = hasMidnightScaling == "1"
    local result = AddonTable.BonusIdAlgorithm.ProcessItem(link, baseItemLevel, hasMidnightScaling)
    io.write(result .. "\n")
    io.flush()
end
