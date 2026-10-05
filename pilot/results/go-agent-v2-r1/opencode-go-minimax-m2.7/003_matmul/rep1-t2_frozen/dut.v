module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
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

// State machine
localparam IDLE    = 2'd0;
localparam RECV    = 2'd1;
localparam COMP    = 2'd2;
localparam OUTPUT  = 2'd3;

reg [1:0] state;
reg [2:0] recv_cnt;
reg [2:0] comp_row;
reg [4:0] comp_k;
reg [2:0] out_cnt;

// Buffer A (M x K), row-major: A[row][col]
reg signed [DATA_W-1:0] a_buf [0:M-1][0:K-1];
// Buffer B (K x N), row-major: B[k][col]
reg signed [DATA_W-1:0] b_buf [0:K-1][0:N-1];

// MAC accumulators for current row (8 parallel MACs, one per output column)
reg signed [ACC_W-1:0] mac_acc [0:N-1];

// Handshake
assign in_a_flat_ready = (state == RECV);
assign in_b_flat_ready = (state == RECV);

// Main FSM
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        state <= IDLE;
        recv_cnt <= 3'd0;
        comp_row <= 3'd0;
        comp_k <= 4'd0;
        out_cnt <= 3'd0;
    end else begin
        case (state)
            IDLE: begin
                recv_cnt <= 3'd0;
                comp_row <= 3'd0;
                comp_k <= 4'd0;
                out_cnt <= 3'd0;
                if (in_a_flat_valid && in_b_flat_valid)
                    state <= RECV;
            end

            RECV: begin
                if (in_a_flat_valid && in_b_flat_valid) begin
                    recv_cnt <= recv_cnt + 3'd1;
                    if (recv_cnt == 3'd7)
                        state <= COMP;
                end
            end

            COMP: begin
                if (comp_k < K - 1) begin
                    comp_k <= comp_k + 4'd1;
                end else begin
                    comp_k <= 4'd0;
                    if (comp_row < M - 1)
                        comp_row <= comp_row + 3'd1;
                end

                if (comp_row == 3'd7 && comp_k == 4'd15)
                    state <= OUTPUT;
            end

            OUTPUT: begin
                if (out_ready) begin
                    out_cnt <= out_cnt + 3'd1;
                    if (out_cnt == 3'd7)
                        state <= IDLE;
                end
            end
        endcase
    end
end

// Store input data in buffers
reg [6:0] a_idx;
reg [6:0] b_idx;

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        a_idx <= 7'd0;
        b_idx <= 7'd0;
    end else begin
        if (state == RECV && in_a_flat_valid && in_b_flat_valid) begin
            // Store A row-major
            for (int lane = 0; lane < LANES; lane = lane + 1) begin
                int row, col;
                row = (a_idx + lane) / K;
                col = (a_idx + lane) % K;
                a_buf[row][col] <= in_a_flat[lane * DATA_W +: DATA_W];
            end
            a_idx <= a_idx + LANES[6:0];

            // Store B row-major
            for (int lane = 0; lane < LANES; lane = lane + 1) begin
                int row, col;
                row = (b_idx + lane) / N;
                col = (b_idx + lane) % N;
                b_buf[row][col] <= in_b_flat[lane * DATA_W +: DATA_W];
            end
            b_idx <= b_idx + LANES[6:0];
        end
        
        if (state == IDLE) begin
            a_idx <= 7'd0;
            b_idx <= 7'd0;
        end
    end
end

// MAC computation
wire [N-1:0] is_first_k;
assign is_first_k = {(N){(comp_k == 4'd0)}};

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        mac_acc[0] <= 0;
        mac_acc[1] <= 0;
        mac_acc[2] <= 0;
        mac_acc[3] <= 0;
        mac_acc[4] <= 0;
        mac_acc[5] <= 0;
        mac_acc[6] <= 0;
        mac_acc[7] <= 0;
    end else if (state == COMP) begin
        if (is_first_k[0]) begin
            mac_acc[0] <= a_buf[comp_row][comp_k] * b_buf[comp_k][0];
        end else begin
            mac_acc[0] <= mac_acc[0] + a_buf[comp_row][comp_k] * b_buf[comp_k][0];
        end
        
        if (is_first_k[1]) begin
            mac_acc[1] <= a_buf[comp_row][comp_k] * b_buf[comp_k][1];
        end else begin
            mac_acc[1] <= mac_acc[1] + a_buf[comp_row][comp_k] * b_buf[comp_k][1];
        end
        
        if (is_first_k[2]) begin
            mac_acc[2] <= a_buf[comp_row][comp_k] * b_buf[comp_k][2];
        end else begin
            mac_acc[2] <= mac_acc[2] + a_buf[comp_row][comp_k] * b_buf[comp_k][2];
        end
        
        if (is_first_k[3]) begin
            mac_acc[3] <= a_buf[comp_row][comp_k] * b_buf[comp_k][3];
        end else begin
            mac_acc[3] <= mac_acc[3] + a_buf[comp_row][comp_k] * b_buf[comp_k][3];
        end
        
        if (is_first_k[4]) begin
            mac_acc[4] <= a_buf[comp_row][comp_k] * b_buf[comp_k][4];
        end else begin
            mac_acc[4] <= mac_acc[4] + a_buf[comp_row][comp_k] * b_buf[comp_k][4];
        end
        
        if (is_first_k[5]) begin
            mac_acc[5] <= a_buf[comp_row][comp_k] * b_buf[comp_k][5];
        end else begin
            mac_acc[5] <= mac_acc[5] + a_buf[comp_row][comp_k] * b_buf[comp_k][5];
        end
        
        if (is_first_k[6]) begin
            mac_acc[6] <= a_buf[comp_row][comp_k] * b_buf[comp_k][6];
        end else begin
            mac_acc[6] <= mac_acc[6] + a_buf[comp_row][comp_k] * b_buf[comp_k][6];
        end
        
        if (is_first_k[7]) begin
            mac_acc[7] <= a_buf[comp_row][comp_k] * b_buf[comp_k][7];
        end else begin
            mac_acc[7] <= mac_acc[7] + a_buf[comp_row][comp_k] * b_buf[comp_k][7];
        end
    end else if (state == IDLE) begin
        mac_acc[0] <= 0;
        mac_acc[1] <= 0;
        mac_acc[2] <= 0;
        mac_acc[3] <= 0;
        mac_acc[4] <= 0;
        mac_acc[5] <= 0;
        mac_acc[6] <= 0;
        mac_acc[7] <= 0;
    end
end

// Output logic
reg signed [ACC_W-1:0] out_c_reg;
reg out_valid_reg;

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        out_valid_reg <= 1'b0;
        out_c_reg <= 0;
    end else begin
        case (state)
            OUTPUT: begin
                out_c_reg <= mac_acc[out_cnt];
                out_valid_reg <= 1'b1;
            end
            default: begin
                out_valid_reg <= 1'b0;
            end
        endcase
    end
end

assign out_c = out_c_reg;
assign out_valid = out_valid_reg;

endmodule
