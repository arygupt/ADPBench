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

    localparam BEATS  = LEN / LANES;
    localparam BEAT_W = LANES * DATA_W;

    // Per-transaction, per-stream beat storage.  A transaction is at most
    // BEATS beats deep on either stream, and both streams are consumed in
    // order, so a linear 0..BEATS-1 buffer is sufficient and cannot wrap.
    reg [BEAT_W-1:0] a_mem [0:BEATS-1];
    reg [BEAT_W-1:0] b_mem [0:BEATS-1];

    // Valid bits for beats already received but not yet consumed.
    reg [BEATS-1:0] a_has;
    reg [BEATS-1:0] b_has;

    reg [3:0] a_wr_count;      // number of a beats received this txn (0..BEATS)
    reg [3:0] b_wr_count;      // number of b beats received this txn (0..BEATS)
    reg [3:0] consume_count;   // number of dot-product beat pairs consumed

    reg signed [ACC_W-1:0] acc;

    wire [3:0] consume_addr = (consume_count < BEATS) ? consume_count : 4'd0;
    wire [BEAT_W-1:0] a_consume_data = a_mem[consume_addr];
    wire [BEAT_W-1:0] b_consume_data = b_mem[consume_addr];

    wire consume_en = (consume_count < BEATS) &&
                      a_has[consume_addr] &&
                      b_has[consume_addr];

    genvar g;
    wire signed [ACC_W-1:0] lane_product [0:LANES-1];
    generate
        for (g = 0; g < LANES; g = g + 1) begin : gen_lane_product
            assign lane_product[g] =
                $signed(a_consume_data[g*DATA_W +: DATA_W]) *
                $signed(b_consume_data[g*DATA_W +: DATA_W]);
        end
    endgenerate

    wire signed [ACC_W-1:0] beat_sum;
    assign beat_sum =
        lane_product[0]  + lane_product[1]  + lane_product[2]  + lane_product[3]  +
        lane_product[4]  + lane_product[5]  + lane_product[6]  + lane_product[7]  +
        lane_product[8]  + lane_product[9]  + lane_product[10] + lane_product[11] +
        lane_product[12] + lane_product[13] + lane_product[14] + lane_product[15] +
        lane_product[16] + lane_product[17] + lane_product[18] + lane_product[19] +
        lane_product[20] + lane_product[21] + lane_product[22] + lane_product[23] +
        lane_product[24] + lane_product[25] + lane_product[26] + lane_product[27] +
        lane_product[28] + lane_product[29] + lane_product[30] + lane_product[31];

    assign in_a_flat_ready = (a_wr_count < BEATS);
    assign in_b_flat_ready = (b_wr_count < BEATS);
    assign out_valid = (consume_count == BEATS);
    assign out_c = acc;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_wr_count    <= 0;
            b_wr_count    <= 0;
            consume_count <= 0;
            acc           <= 0;
            a_has         <= 0;
            b_has         <= 0;
        end else if (out_valid && out_ready) begin
            // End of transaction: clear per-transaction state.  Inputs are
            // held off during this cycle because the write counts are BEATS.
            a_wr_count    <= 0;
            b_wr_count    <= 0;
            consume_count <= 0;
            acc           <= 0;
            a_has         <= 0;
            b_has         <= 0;
        end else begin
            if (in_a_flat_valid && in_a_flat_ready) begin
                a_mem[a_wr_count] <= in_a_flat;
                a_has[a_wr_count] <= 1'b1;
                a_wr_count        <= a_wr_count + 1;
            end

            if (in_b_flat_valid && in_b_flat_ready) begin
                b_mem[b_wr_count] <= in_b_flat;
                b_has[b_wr_count] <= 1'b1;
                b_wr_count        <= b_wr_count + 1;
            end

            if (consume_en) begin
                acc                   <= acc + beat_sum;
                a_has[consume_addr]   <= 1'b0;
                b_has[consume_addr]   <= 1'b0;
                consume_count         <= consume_count + 1;
            end
        end
    end

endmodule
