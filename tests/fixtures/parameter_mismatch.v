// Sanity-check submission: lane-parallel dot product.
// One multiplier per lane, beat summed combinationally, single-cycle MAC when
// both ports present, small hold registers for independently-arriving beats.
// Handles repeated transactions: all state is cleared when the output is
// accepted.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 1,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
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

    localparam BEATS  = LEN / LANES;
    localparam BEAT_W = $clog2(BEATS);

    reg signed [ACC_W-1:0] acc;
    reg [BEAT_W-1:0]       beat;
    reg                    draining;
    reg                    out_valid_r;

    reg                    a_have;
    reg                    b_have;
    reg [LANES*DATA_W-1:0] a_hold;
    reg [LANES*DATA_W-1:0] b_hold;

    wire [LANES*DATA_W-1:0] a_eff = a_have ? a_hold : in_a_flat;
    wire [LANES*DATA_W-1:0] b_eff = b_have ? b_hold : in_b_flat;
    wire a_present = a_have || in_a_flat_valid;
    wire b_present = b_have || in_b_flat_valid;
    wire fire      = rst_n && !draining && !out_valid_r && a_present && b_present;

    assign in_a_flat_ready = rst_n && !a_have && !out_valid_r && !draining;
    assign in_b_flat_ready = rst_n && !b_have && !out_valid_r && !draining;
    assign out_valid       = out_valid_r;
    assign out_c           = acc;

    // Sum the LANES products of the beat currently on the effective bus.
    reg signed [ACC_W-1:0] beat_sum;
    integer k;
    always @(*) begin
        beat_sum = {ACC_W{1'b0}};
        for (k = 0; k < LANES; k = k + 1) begin
            beat_sum = beat_sum
                     + $signed(a_eff[k*DATA_W +: DATA_W])
                     * $signed(b_eff[k*DATA_W +: DATA_W]);
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            acc         <= {ACC_W{1'b0}};
            beat        <= {BEAT_W{1'b0}};
            draining    <= 1'b0;
            out_valid_r <= 1'b0;
            a_have      <= 1'b0;
            b_have      <= 1'b0;
        end else begin
            if (fire) begin
                acc    <= acc + beat_sum;
                a_have <= 1'b0;
                b_have <= 1'b0;
                if (beat == BEATS - 1) begin
                    beat     <= {BEAT_W{1'b0}};
                    draining <= 1'b1;
                end else begin
                    beat <= beat + 1'b1;
                end
            end else begin
                if (!a_have && in_a_flat_valid && in_a_flat_ready) begin
                    a_hold <= in_a_flat;
                    a_have <= 1'b1;
                end
                if (!b_have && in_b_flat_valid && in_b_flat_ready) begin
                    b_hold <= in_b_flat;
                    b_have <= 1'b1;
                end
            end

            if (draining) begin
                draining    <= 1'b0;
                out_valid_r <= 1'b1;
            end

            if (out_valid_r && out_ready) begin
                out_valid_r <= 1'b0;
                acc         <= {ACC_W{1'b0}};
                beat        <= {BEAT_W{1'b0}};
                a_have      <= 1'b0;
                b_have      <= 1'b0;
            end
        end
    end

endmodule
