module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    // LANES per beat, LEN/LANES beats per transaction
    localparam BEATS = LEN / LANES;   // 8
    localparam DEPTH = 4;             // slot buffer per stream

    // small per-stream slot buffers (independent handshakes)
    reg [LANES*DATA_W-1:0] amem [0:DEPTH-1];
    reg [LANES*DATA_W-1:0] bmem [0:DEPTH-1];
    reg [1:0] acnt, bcnt, afront, bfront;

    wire afull = (acnt == 2'd3);
    wire bfull = (bcnt == 2'd3);

    assign in_a_flat_ready = !afull;
    assign in_b_flat_ready = !bfull;

    wire awr = in_a_flat_valid && !afull;
    wire bwr = in_b_flat_valid && !bfull;

    wire hold = out_valid && !out_ready;
    wire ado = (acnt != 2'd0);
    wire bdo = (bcnt != 2'd0);
    wire do_p = ado && bdo && !hold;

    // dot product of one beat (32 signed int8 products summed)
    function signed [ACC_W-1:0] beat_dot;
        input [LANES*DATA_W-1:0] a, b;
        integer k;
        reg signed [DATA_W-1:0] x, y;
        begin
            beat_dot = {ACC_W{1'b0}};
            for (k = 0; k < LANES; k = k + 1) begin
                x = a[k*DATA_W +: DATA_W];
                y = b[k*DATA_W +: DATA_W];
                beat_dot = beat_dot + x * y;
            end
        end
    endfunction

    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] outreg;
    reg outv;
    reg [2:0] beatcnt;
    wire last = do_p && (beatcnt == BEATS[2:0] - 3'd1);

    assign out_valid = outv;
    assign out_c = outreg;

    always @(posedge clk) begin
        if (!rst_n) begin
            acnt <= 0; bcnt <= 0; afront <= 0; bfront <= 0;
            acc <= 0; outv <= 0; outreg <= 0; beatcnt <= 0;
        end else begin
            // stream A: write / pop
            if (awr) begin
                amem[(afront + acnt) & 2'b11] <= in_a_flat;
                if (!do_p) acnt <= acnt + 2'd1;
            end
            if (do_p) begin
                afront <= afront + 2'd1;
                if (!awr) acnt <= acnt - 2'd1;
            end
            // stream B: write / pop
            if (bwr) begin
                bmem[(bfront + bcnt) & 2'b11] <= in_b_flat;
                if (!do_p) bcnt <= bcnt + 2'd1;
            end
            if (do_p) begin
                bfront <= bfront + 2'd1;
                if (!bwr) bcnt <= bcnt - 2'd1;
            end
            // accumulator / output staging
            if (last) begin
                acc     <= 0;            // reset for next transaction
                outreg  <= acc + beat_dot(amem[afront], bmem[bfront]);
                outv    <= 1;
                beatcnt <= 0;
            end else if (do_p) begin
                acc      <= acc + beat_dot(amem[afront], bmem[bfront]);
                beatcnt  <= beatcnt + 3'd1;
            end
            if (out_valid && out_ready && !last)
                outv <= 1'b0;
            if (out_valid && out_ready && !do_p)
                outv <= 1'b0;
        end
    end
endmodule
