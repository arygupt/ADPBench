// Offline check for the tiny provider-compatibility prompt, NOT a benchmark.
`timescale 1ns/1ps
module canary_xor_tb;
  reg a, b;
  wire y;
  integer i;
  dut uut(.a(a), .b(b), .y(y));
  initial begin
    for (i = 0; i < 4; i = i + 1) begin
      {a, b} = i[1:0];
      #1;
      if (y !== (a ^ b)) $fatal(1, "XOR mismatch at input %0d", i);
    end
    $display("PASS: all 4 XOR input combinations");
    $finish;
  end
  initial begin
    #10;
    $fatal(1, "XOR diagnostic timeout");
  end
endmodule
