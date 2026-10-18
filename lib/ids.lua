-- Ids are positive integers.
local next_id = 0

return function()
  next_id = next_id + 1
  return next_id
end
