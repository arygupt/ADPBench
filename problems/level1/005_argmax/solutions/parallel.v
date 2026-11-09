// One beat per cycle: a balanced comparison tree picks the earliest lane.
// Fixed benchmark LANES is a power of two; no whole-row storage is needed.
module dut #(
    parameter ROWS = 8, COLS = 64, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire [ACC_W-1:0] out_c
);
    localparam BEATS = COLS / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam LW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam IW = (COLS > 1) ? $clog2(COLS) : 1;
    localparam signed [DATA_W-1:0] MIN_VALUE = {1'b1, {(DATA_W-1){1'b0}}};
    wire signed [DATA_W-1:0] tree_value [1:2*LANES-1];
    wire [LW-1:0] tree_index [1:2*LANES-1];
    genvar n;
    generate
        for (n = 0; n < LANES; n = n + 1) begin: leaves
            assign tree_value[LANES+n] = in_x_flat[n*DATA_W +: DATA_W];
            assign tree_index[LANES+n] = n;
        end
        for (n = 1; n < LANES; n = n + 1) begin: branches
            wire left_wins = tree_value[2*n] >= tree_value[2*n+1];
            assign tree_value[n] = left_wins ? tree_value[2*n] : tree_value[2*n+1];
            assign tree_index[n] = left_wins ? tree_index[2*n] : tree_index[2*n+1];
        end
    endgenerate

    reg sending;
    reg [BW-1:0] beat;
    reg signed [DATA_W-1:0] best;
    reg [IW-1:0] best_index;
    assign in_x_flat_ready = !sending;
    assign out_valid = sending;
    assign out_c = best_index;

    always @(posedge clk) begin
        if (!rst_n) begin
            sending <= 1'b0;
            beat <= 0;
            best <= MIN_VALUE;
            best_index <= 0;
        end else if (sending) begin
            if (out_ready) begin
                sending <= 1'b0;
                best <= MIN_VALUE;
                best_index <= 0;
            end
        end else if (in_x_flat_valid) begin
            if (beat == 0 || tree_value[1] > best) begin
                best <= tree_value[1];
                best_index <= beat * LANES + tree_index[1];
            end
            if (beat == BEATS - 1) begin
                beat <= 0;
                sending <= 1'b1;
            end else beat <= beat + 1'b1;
        end
    end
endmodule
