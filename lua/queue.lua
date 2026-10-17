local M = {}

function M.push(queue, item)
  table.insert(queue, item)
end

function M.pop(queue)
  return table.remove(queue, 1)
end

return M
