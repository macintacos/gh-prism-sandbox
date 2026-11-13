local M = {}

--[[
Queues are plain tables.
]]
function M.push(queue, item)
  -- Append at the tail.
  queue[#queue + 1] = item
end

function M.pop(queue)
  return table.remove(queue, 1)
end

return M
