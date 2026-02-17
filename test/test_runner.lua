package.path = "../LibBonusId/?.lua;" .. package.path

-- WoW Lua Environment
_G.floor = math.floor
_G.min = math.min
_G.max = math.max
_G.gmatch = string.gmatch
_G.strmatch = string.match
_G.sort = table.sort
_G.tinsert = table.insert
_G.tremove = table.remove
_G.wipe = function(t) for i in pairs(t) do t[i] = nil end end
function GetBuildInfo()
    return "12.0.1", "65893"
end

require("LibStub.LibStub")
require("LibBonusId")
local LibBonusId = LibStub("LibBonusId")

local data_path = arg[1]
local f = loadfile(data_path)
if not f then
    io.stderr:write("Failed to load " .. data_path .. "\n")
    os.exit(1)
end
f()

local function parseBonusString(str)
    local parts = {}
    for p in string.gmatch(str, "(%d+)") do
        tinsert(parts, tonumber(p))
    end
    local numBonuses = parts[1]
    local bonusIds = {}
    for i = 2, 1 + numBonuses do
        tinsert(bonusIds, parts[i])
    end
    local dropLevel = nil
    local idx = 2 + numBonuses
    if parts[idx] then
        local numModifiers = parts[idx]
        idx = idx + 1
        for _ = 1, numModifiers do
            local modType = parts[idx]
            local modValue = parts[idx + 1]
            if modType == 9 then
                dropLevel = modValue
            end
            idx = idx + 2
        end
    end
    return bonusIds, dropLevel
end

for line in io.lines() do
    local rtLink = string.match(line, "^BONUS_RT:(.+)$")
    if rtLink then
        local itemLevel = LibBonusId.CalculateItemLevelFromItemLink(rtLink)
        local bonusString = LibBonusId.GetBonusStringForLevel(itemLevel)
        if not bonusString then
            io.write(itemLevel .. "\tNIL\n")
        else
            local itemId = tonumber(string.match(rtLink, "item:(%d+)"))
            local bonusIds, dropLevel = parseBonusString(bonusString)
            local result = LibBonusId.CalculateItemLevelFromItemInfo(itemId, bonusIds, dropLevel, nil)
            io.write(itemLevel .. "\t" .. bonusString .. "\t" .. result .. "\n")
        end
    else
        local result = LibBonusId.CalculateItemLevelFromItemLink(line)
        io.write(result .. "\n")
    end
    io.flush()
end
