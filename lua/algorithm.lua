local floor = math.floor
local min = math.min
local max = math.max
local sort = table.sort

local OP_GROUP = { scale = "S", set = "S", add = "Q" }

local function interpolate(curve, value)
    local lowerBound, upperBound = -math.huge, math.huge
    for level, itemLevel in pairs(curve) do
        if level == value then
            return floor(itemLevel + 0.5)
        elseif level < value then
            lowerBound = max(lowerBound, level)
        else
            upperBound = min(upperBound, level)
        end
    end
    if lowerBound == -math.huge then
        return floor(curve[upperBound] + 0.5)
    elseif upperBound == math.huge then
        return floor(curve[lowerBound] + 0.5)
    end
    return floor(curve[lowerBound] + (value - lowerBound) / (upperBound - lowerBound)
        * (curve[upperBound] - curve[lowerBound]) + 0.5)
end

local function parseLink(link)
    -- Split on ':'
    local allParts = {}
    for part in (link .. ":"):gmatch("([^:]*):") do
        allParts[#allParts + 1] = part
    end

    -- Variable-length section starts at index 15 (1-indexed)
    -- Filter out parts starting with "|h"
    local parts = {}
    for i = 15, #allParts do
        local p = allParts[i]
        if p:sub(1, 2) ~= "|h" then
            parts[#parts + 1] = p
        end
    end

    local idx = 1

    local numBonusIds = 0
    if parts[idx] and parts[idx] ~= "" then
        numBonusIds = tonumber(parts[idx])
    end
    idx = idx + 1

    local bonusIds = {}
    for _ = 1, numBonusIds do
        bonusIds[#bonusIds + 1] = tonumber(parts[idx])
        idx = idx + 1
    end

    local numModifiers = 0
    if parts[idx] and parts[idx] ~= "" then
        numModifiers = tonumber(parts[idx])
    end
    idx = idx + 1

    local playerLevel = 0
    local contentTuningId = 0
    for _ = 1, numModifiers do
        local modType = tonumber(parts[idx])
        local modValue = tonumber(parts[idx + 1])
        idx = idx + 2
        if modType == 9 then
            playerLevel = modValue
        elseif modType == 28 then
            contentTuningId = modValue
        end
    end

    return bonusIds, playerLevel, contentTuningId
end

local M = {}

function M.new(data)
    local bonuses = data.bonuses
    local curves = data.curves
    local squishCurveIndex = data.squish_curve
    local squishCurve = curves[squishCurveIndex + 1]
    local contentTuning = data.content_tuning

    -- Pre-compute max squish key
    local squishMax = -math.huge
    for k in pairs(squishCurve) do
        if k > squishMax then squishMax = k end
    end

    -- Expand CT remap
    if data.ct_remap then
        for src, dst in pairs(data.ct_remap) do
            if contentTuning[dst] then
                contentTuning[src] = contentTuning[dst]
            end
        end
    end

    local algo = {}

    local function getSquishValue(value)
        if value > squishMax then
            return 1
        end
        return interpolate(squishCurve, value)
    end

    local function getCurveValue(curveId, value)
        return interpolate(curves[curveId + 1], value)
    end

    local function applyContentTuning(dropLevel, ctId, ctKey)
        local ct = contentTuning[ctId]
        if not ct then return dropLevel end
        local op = ct[ctKey] or ct.op
        if not op then return dropLevel end
        local name = op[1]
        if name == "cap" then
            return min(dropLevel, op[2])
        elseif name == "clamp" then
            return min(max(dropLevel, op[2]), op[3])
        elseif name == "const" then
            return op[2]
        elseif name == "cap_add" then
            return min(dropLevel, op[2]) + op[3]
        elseif name == "cap_add_floor" then
            return max(min(dropLevel, op[2]) + op[3], op[4])
        end
        return dropLevel
    end

    local function getBonusIds(rawBonusIds)
        local result = {}
        for _, id in ipairs(rawBonusIds) do
            local d = bonuses[id]
            if d and d.redirect then
                result[#result + 1] = d.redirect
            else
                result[#result + 1] = id
            end
        end
        sort(result, function(a, b)
            local spA = (bonuses[a] or {}).sp or 0
            local spB = (bonuses[b] or {}).sp or 0
            if spA ~= spB then return spA < spB end
            return a < b
        end)
        return result
    end

    function algo.processItem(link, baseItemLevel, hasMidnightScaling)
        local rawBonusIds, playerLevel, contentTuningId = parseLink(link)

        local itemLevel = baseItemLevel
        local midnightScaling = hasMidnightScaling

        local collected = {}
        local bonusIdList = getBonusIds(rawBonusIds)

        local function collectBonus(bonus)
            if bonus.midnight == "set" then
                midnightScaling = true
            end

            local group = OP_GROUP[bonus.op]
            if not group then
                collected[#collected + 1] = bonus
                return
            end

            local prevIndex
            for i, b in ipairs(collected) do
                if OP_GROUP[b.op] == group then
                    prevIndex = i
                    break
                end
            end

            if not prevIndex then
                collected[#collected + 1] = bonus
            else
                local prevPrio = collected[prevIndex].priority
                local newPrio = bonus.priority
                if prevPrio ~= nil and newPrio ~= nil then
                    if newPrio <= prevPrio then
                        collected[prevIndex] = bonus
                    end
                else
                    collected[prevIndex] = bonus
                end
            end
        end

        local function collectAll(bonusId)
            local d = bonuses[bonusId]
            if not d or d.redirect then return end
            collectBonus(d)
            if d.other then
                collectBonus(d.other)
            end
        end

        -- Collect indirect first, then direct (direct overrides via dedup)
        for _, bonusId in ipairs(bonusIdList) do
            local d = bonuses[bonusId]
            if d and d.indirect then
                collectAll(bonusId)
            end
        end
        for _, bonusId in ipairs(bonusIdList) do
            local d = bonuses[bonusId]
            if d and not d.indirect then
                collectAll(bonusId)
            end
        end

        if #collected == 0 then
            if not midnightScaling then
                itemLevel = getSquishValue(itemLevel)
            end
            return itemLevel
        end

        for _, bonus in ipairs(collected) do
            local op = bonus.op
            if op == "legacy_add" then
                itemLevel = itemLevel + bonus.amount
            elseif op == "add" then
                if bonus.midnight == "force" and not midnightScaling then
                    midnightScaling = true
                    itemLevel = getSquishValue(itemLevel)
                end
                itemLevel = itemLevel + bonus.amount
            elseif op == "set" then
                itemLevel = bonus.item_level
            elseif op == "scale" then
                local dropLevel
                if bonus.default_level and bonus.default_level ~= 0 then
                    dropLevel = bonus.default_level
                elseif playerLevel ~= 0 then
                    dropLevel = playerLevel
                else
                    dropLevel = 80
                end

                if bonus.ct_key then
                    local ct = contentTuningId
                    if ct == 0 then ct = bonus.ct_id end
                    if ct and ct ~= 0
                        and (not bonus.ct_default_only or playerLevel == 0)
                    then
                        dropLevel = applyContentTuning(dropLevel, ct, bonus.ct_key)
                    end
                end
                itemLevel = getCurveValue(bonus.curve_id, dropLevel) + (bonus.offset or 0)
            end

            -- Post-op midnight handling
            local midnight = bonus.midnight
            if midnight == "set" then
                midnightScaling = true
            elseif midnight == "squish" and midnightScaling then
                itemLevel = getSquishValue(itemLevel)
            end
        end

        itemLevel = max(itemLevel, 1)
        if not midnightScaling then
            itemLevel = getSquishValue(itemLevel)
        end
        return itemLevel
    end

    return algo
end

return M
