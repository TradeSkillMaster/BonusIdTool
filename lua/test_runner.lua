local algo = require("lua.algorithm")

local data_path = arg[1]
local f = loadfile(data_path)
if not f then
    io.stderr:write("Failed to load " .. data_path .. "\n")
    os.exit(1)
end
local data = f()
local engine = algo.new(data)

for line in io.lines() do
    local link, baseItemLevel, hasMidnightScaling = line:match("^(.-)\t(.-)\t(.-)$")
    baseItemLevel = tonumber(baseItemLevel)
    hasMidnightScaling = hasMidnightScaling == "1"
    local result = engine.processItem(link, baseItemLevel, hasMidnightScaling)
    io.write(result .. "\n")
    io.flush()
end
