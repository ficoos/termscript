wait: prompt
type: this is termscript
sleep: 3
type: <el>
type: it allows you to create terminal demos without ma\bistaks\bes or typoo\bs
sleep: 3
type: <el>
# enter - type command followed by '\n' and wait for prompt
enter: echo you can execute commands
sleep: 3
enter: echo and even interact with complex applications
sleep: 3
# type - enter input as written. Can be escaped
type: vim\n
# sleep - wait for some time before executing next command
sleep: 4
type: |
  ihello world
  this is multiline<esc>
sleep: 3
type: :qa!\n
wait: prompt
type: you can even simulate special key-strokes
sleep: 3
enter: <el><up><up>
enter: exit
