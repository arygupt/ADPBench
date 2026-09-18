// Sanity-check submission: lane-parallel dot product.
// Not a baseline and not part of the task set. It exists to prove the harness
// rewards a genuinely better design: one multiplier per lane, beat summed
// combinationally, pipelined accumulation. Compare its score to baseline.v.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 1,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire                    in_valid,
    output wire                    in_ready,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire [LANES*DATA_W-1:0] in_b_flat,

    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS  = LEN / LANES;
    localparam BEAT_W = $clog2(BEATS);

    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] beat_sum_r;
    reg [BEAT_W-1:0]       beat;
    reg                    draining;
    reg                    out_valid_r;

    // Sum the LANES products of the beat currently on the bus.
    reg signed [ACC_W-1:0] beat_sum;
    integer k;
    always @(*) begin
        beat_sum = {ACC_W{1'b0}};
        for (k = 0; k < LANES; k = k + 1) begin
            beat_sum = beat_sum
                     + $signed(in_a_flat[k*DATA_W +: DATA_W])
                     * $signed(in_b_flat[k*DATA_W +: DATA_W]);
        end
    end

    assign in_ready  = rst_n && !draining && !out_valid_r;
    assign out_valid = out_valid_r;
    assign out_c     = acc;

    always @(posedge clk) begin
        if (!rst_n) begin
            acc         <= {ACC_W{1'b0}};
            beat_sum_r  <= {ACC_W{1'b0}};
            beat        <= {BEAT_W{1'b0}};
            draining    <= 1'b0;
            out_valid_r <= 1'b0;
        end else begin
            if (in_valid && in_ready) begin
                beat_sum_r <= beat_sum;
                if (beat == BEATS - 1) begin
                    beat     <= {BEAT_W{1'b0}};
                    draining <= 1'b1;
                end else begin
                    beat <= beat + 1'b1;
                end
            end

            // One accumulate per accepted beat, plus one flush for the last.
            if ((in_valid && in_ready) || draining)
                acc <= acc + beat_sum_r;

            if (draining) begin
                draining    <= 1'b0;
                out_valid_r <= 1'b1;
            end

            if (out_valid_r && out_ready) begin
                out_valid_r <= 1'b0;
                acc         <= {ACC_W{1'b0}};
            end
        end
    end

endmodule
