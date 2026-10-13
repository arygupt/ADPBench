// Serial repeated selection: buffer the vector, scan it K times, marking
// selected positions. A used mask preserves duplicate values, including -128.
module dut #(
    parameter LEN = 64, K = 8, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam BEATS = LEN / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam IW = (LEN > 1) ? $clog2(LEN) : 1;
    localparam KW = (K > 1) ? $clog2(K) : 1;
    localparam RECV = 2'd0, SCAN = 2'd1, SEND = 2'd2;
    localparam signed [DATA_W-1:0] MIN_VALUE = {1'b1, {(DATA_W-1){1'b0}}};
    reg [1:0] state;
    reg [BW-1:0] beat;
    reg [IW-1:0] scan_index, best_index;
    reg [KW-1:0] emitted;
    reg signed [DATA_W-1:0] values [0:LEN-1];
    reg [LEN-1:0] used;
    reg have_best;
    reg signed [DATA_W-1:0] best, out_value;
    wire take = !used[scan_index] && (!have_best || values[scan_index] > best);
    wire [IW-1:0] selected_index = take ? scan_index : best_index;
    wire signed [DATA_W-1:0] selected_value = take ? values[scan_index] : best;
    integer j;

    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = out_value;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            beat <= 0;
            scan_index <= 0;
            best_index <= 0;
            emitted <= 0;
            used <= 0;
            have_best <= 1'b0;
            best <= MIN_VALUE;
            out_value <= MIN_VALUE;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    for (j = 0; j < LANES; j = j + 1)
                        values[beat*LANES+j] <= in_x_flat[j*DATA_W +: DATA_W];
                    if (beat == BEATS - 1) begin
                        beat <= 0;
                        state <= SCAN;
                    end else beat <= beat + 1'b1;
                end
                SCAN: begin
                    if (take) begin
                        best <= values[scan_index];
                        best_index <= scan_index;
                        have_best <= 1'b1;
                    end
                    if (scan_index == LEN - 1) begin
                        out_value <= selected_value;
                        used[selected_index] <= 1'b1;
                        scan_index <= 0;
                        state <= SEND;
                    end else scan_index <= scan_index + 1'b1;
                end
                SEND: if (out_ready) begin
                    best <= MIN_VALUE;
                    have_best <= 1'b0;
                    if (emitted == K - 1) begin
                        emitted <= 0;
                        used <= 0;
                        state <= RECV;
                    end else begin
                        emitted <= emitted + 1'b1;
                        state <= SCAN;
                    end
                end
                default: state <= RECV;
            endcase
        end
    end
endmodule
