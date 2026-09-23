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

    localparam BEATS = LEN / LANES;
    localparam BEAT_CNT_W = $clog2(BEATS);

    reg [LANES*DATA_W-1:0] a_buf, b_buf;
    reg a_buf_valid, b_buf_valid;
    reg signed [ACC_W-1:0] acc;
    reg [BEAT_CNT_W-1:0] beat_cnt;
    reg out_valid_r;
    reg signed [ACC_W-1:0] out_c_r;

    // Data actually used for multiplication: buffered value if available, else input
    wire [LANES*DATA_W-1:0] a_used = a_buf_valid ? a_buf : in_a_flat;
    wire [LANES*DATA_W-1:0] b_used = b_buf_valid ? b_buf : in_b_flat;

    wire a_avail = a_buf_valid | in_a_flat_valid;
    wire b_avail = b_buf_valid | in_b_flat_valid;
    wire do_compute = !out_valid_r && (beat_cnt < BEATS) && a_avail && b_avail;

    // Independent ready signals: accept a beat only when the corresponding buffer is free
    assign in_a_flat_ready = !out_valid_r && (beat_cnt < BEATS) && !a_buf_valid;
    assign in_b_flat_ready = !out_valid_r && (beat_cnt < BEATS) && !b_buf_valid;

    // Combinational sum of products for one beat
    reg signed [ACC_W-1:0] prod_sum;
    integer k;
    always @(*) begin
        prod_sum = 0;
        for (k = 0; k < LANES; k = k + 1) begin
            prod_sum = prod_sum + $signed(a_used[k*DATA_W +: DATA_W]) * $signed(b_used[k*DATA_W +: DATA_W]);
        end
    end

    assign out_valid = out_valid_r;
    assign out_c = out_c_r;

    always @(posedge clk) begin
        if (!rst_n) begin
            a_buf_valid <= 1'b0;
            b_buf_valid <= 1'b0;
            acc <= 0;
            beat_cnt <= 0;
            out_valid_r <= 1'b0;
            out_c_r <= 0;
        end else begin
            if (do_compute) begin
                acc <= acc + prod_sum;
                beat_cnt <= beat_cnt + 1'b1;
                if (beat_cnt == BEATS-1) begin
                    out_valid_r <= 1'b1;
                    out_c_r <= acc + prod_sum;
                end
                a_buf_valid <= 1'b0;
                b_buf_valid <= 1'b0;
            end else begin
                if (in_a_flat_valid && in_a_flat_ready) begin
                    a_buf <= in_a_flat;
                    a_buf_valid <= 1'b1;
                end
                if (in_b_flat_valid && in_b_flat_ready) begin
                    b_buf <= in_b_flat;
                    b_buf_valid <= 1'b1;
                end
            end

            // Output handshake: clear transaction state for the next transaction
            if (out_valid_r && out_ready) begin
                out_valid_r <= 1'b0;
                acc <= 0;
                beat_cnt <= 0;
                a_buf_valid <= 1'b0;
                b_buf_valid <= 1'b0;
            end
        end
    end

endmodule
