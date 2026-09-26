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

    localparam BEATS = LEN / LANES;          // 8 beats per stream per transaction
    localparam CNT_W = 3;                    // counts 0..BEATS-1

    // -------------------------------------------------------------
    // Per-stream elastic slot (depth 1) so each stream can run ahead
    // of the other without deadlock or data loss.
    // -------------------------------------------------------------
    reg [LANES*DATA_W-1:0] buf_a;
    reg [LANES*DATA_W-1:0] buf_b;
    reg                    full_a;
    reg                    full_b;

    wire [LANES*DATA_W-1:0] dataA = full_a ? buf_a : in_a_flat;
    wire [LANES*DATA_W-1:0] dataB = full_b ? buf_b : in_b_flat;

    wire availA = full_a | in_a_flat_valid;
    wire availB = full_b | in_b_flat_valid;

    // -----------------------------------------------
    // Handshake / consumption control
    // -----------------------------------------------
    reg  [CNT_W-1:0] cnt;    // pair index within current transaction
    reg  signed [ACC_W-1:0] acc;
    reg  signed [ACC_W-1:0] out_r;
    reg                out_pend;

    wire last_pending = out_pend & (cnt == (BEATS-1));  // hold last pair until result accepted
    wire go_raw       = availA & availB;
    wire take         = go_raw & ~last_pending;         // consume one pair this cycle

    assign in_a_flat_ready = (~full_a) | take;
    assign in_b_flat_ready = (~full_b) | take;

    assign out_valid = out_pend;
    assign out_c     = out_r;

    // -------------------------------------------------------------
    // Signed 8x8 products, LANES per pair-consumption cycle
    // -------------------------------------------------------------
    wire signed [DATA_W-1:0] a_l [0:LANES-1];
    wire signed [DATA_W-1:0] b_l [0:LANES-1];
    wire signed [2*DATA_W-1:0] p   [0:LANES-1];   // 16-bit signed product
    wire signed [2*DATA_W:0]   s1  [0:LANES/2-1]; // 17-bit
    wire signed [2*DATA_W+2:0] s2  [0:LANES/4-1]; // 19-bit
    wire signed [2*DATA_W+4:0] s3  [0:LANES/8-1]; // 21-bit
    wire signed [2*DATA_W+6:0] s4  [0:LANES/16-1];// 23-bit
    wire signed [2*DATA_W+8:0] s5;                // 25-bit (full 32-lane sum)
    wire signed [ACC_W-1:0]    beat_sum;

    genvar i;
    generate
        for (i = 0; i < LANES; i = i + 1) begin : G_LANE
            assign a_l[i] = dataA[i*DATA_W +: DATA_W];
            assign b_l[i] = dataB[i*DATA_W +: DATA_W];
            assign p[i]   = $signed(a_l[i]) * $signed(b_l[i]);
        end
        for (i = 0; i < LANES/2; i = i + 1) begin : G_S1
            assign s1[i] = p[2*i] + p[2*i+1];
        end
        for (i = 0; i < LANES/4; i = i + 1) begin : G_S2
            assign s2[i] = s1[2*i] + s1[2*i+1];
        end
        for (i = 0; i < LANES/8; i = i + 1) begin : G_S3
            assign s3[i] = s2[2*i] + s2[2*i+1];
        end
        for (i = 0; i < LANES/16; i = i + 1) begin : G_S4
            assign s4[i] = s3[2*i] + s3[2*i+1];
        end
    endgenerate

    assign s5       = s4[0] + s4[1];
    assign beat_sum = s5;   // sign-extends 25 -> ACC_W

    // -------------------------------------------------------------
    // Sequential logic
    // -------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            full_a   <= 1'b0;
            full_b   <= 1'b0;
            out_pend <= 1'b0;
            cnt      <= {CNT_W{1'b0}};
            acc      <= {ACC_W{1'b0}};
            out_r    <= {ACC_W{1'b0}};
            buf_a    <= {LANES*DATA_W{1'b0}};
            buf_b    <= {LANES*DATA_W{1'b0}};
        end else begin
            // ---- accumulator / transaction counter ----
            if (take) begin
                if (cnt == (BEATS-1)) begin
                    out_r    <= acc + beat_sum;
                    acc      <= {ACC_W{1'b0}};
                    cnt      <= {CNT_W{1'b0}};
                    out_pend <= 1'b1;
                end else begin
                    acc <= acc + beat_sum;
                    cnt <= cnt + 1'b1;
                end
            end

            // ---- output handshake ----
            if (out_pend && out_ready)
                out_pend <= 1'b0;

            // ---- stream A slot ----
            if (full_a && take) begin
                full_a <= in_a_flat_valid;
                buf_a  <= in_a_flat;
            end else if (!full_a && in_a_flat_valid && !take) begin
                full_a <= 1'b1;
                buf_a  <= in_a_flat;
            end
            // (empty slot consumed directly on a take: nothing stored)

            // ---- stream B slot ----
            if (full_b && take) begin
                full_b <= in_b_flat_valid;
                buf_b  <= in_b_flat;
            end else if (!full_b && in_b_flat_valid && !take) begin
                full_b <= 1'b1;
                buf_b  <= in_b_flat;
            end
        end
    end

endmodule
